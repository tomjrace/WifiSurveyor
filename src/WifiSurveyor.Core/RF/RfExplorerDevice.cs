using System.Diagnostics;
using System.Globalization;
using System.IO.Ports;
using System.Text;
using WifiSurveyor.Models;

namespace WifiSurveyor.RF;

public sealed record RfExplorerConfig(
    double StartMHz,
    double StepMHz,
    int AmpTop,
    int AmpBottom,
    int SweepPoints,
    bool ExpansionActive,
    int Mode,
    double MinMHz,
    double MaxMHz,
    double MaxSpanMHz,
    double RbwKHz)
{
    public double EndMHz => StartMHz + StepMHz * Math.Max(0, SweepPoints - 1);
}

/// <summary>
/// Minimal RF Explorer UART driver.
///
/// Protocol notes (verify against RF Explorer's published UART API document):
///   PC → device : '#', total-length byte, ASCII command. "C0" = request config,
///                 "C2-F:SSSSSSS,EEEEEEE,-TTT,-BBB" = set sweep (kHz / dBm),
///                 "CM" + 0x00/0x01 = switch to main / expansion module.
///   device → PC : "#C2-F:..." current config line, "#C2-M:..." model line,
///                 "$S" + count byte + count bytes of sweep data (dBm = −byte/2),
///                 "$s" + byte, where count = (byte+1)·16 (firmware with larger sweeps).
/// </summary>
public sealed class RfExplorerDevice : IDisposable
{
    readonly SerialPort _port;
    readonly object _writeLock = new();
    Thread? _reader;
    volatile bool _running;

    public RfExplorerDevice(string portName, int baud = 500000)
    {
        _port = new SerialPort(portName, baud, Parity.None, 8, StopBits.One)
        {
            Handshake = Handshake.None,
            ReadTimeout = 500,
            WriteTimeout = 1000,
            ReadBufferSize = 64 * 1024
        };
    }

    public string PortName => _port.PortName;
    public bool IsOpen => _running && _port.IsOpen;
    public RfExplorerConfig? Config { get; private set; }
    public string? ModelInfo { get; private set; }

    public event Action<RfExplorerConfig>? ConfigReceived;
    public event Action<double[], RfExplorerConfig>? SweepReceived;
    public event Action<string>? Disconnected;

    public void Open()
    {
        _port.Open();
        _port.DiscardInBuffer();
        _running = true;
        _reader = new Thread(ReadLoop) { IsBackground = true, Name = "RF Explorer reader" };
        _reader.Start();
        RequestConfig();
    }

    public void RequestConfig() => SendText("C0");

    public void SetSweep(double startMHz, double endMHz, int ampTop = -10, int ampBottom = -120)
    {
        long s = (long)Math.Round(startMHz * 1000), e = (long)Math.Round(endMHz * 1000);
        SendText(string.Create(CultureInfo.InvariantCulture,
            $"C2-F:{s:D7},{e:D7},{ampTop:000},{ampBottom:000}"));
    }

    public void SwitchModule(bool expansion) =>
        Send(new[] { (byte)'C', (byte)'M', (byte)(expansion ? 1 : 0) });

    /// <summary>Waits for the device to report its configuration; null if it doesn't answer.</summary>
    public async Task<RfExplorerConfig?> GetConfigAsync(TimeSpan timeout)
    {
        try { return await WaitForConfigAsync(_ => true, RequestConfig, timeout, CancellationToken.None); }
        catch (TimeoutException) { return null; }
    }

    /// <summary>Selects the module that covers the range and applies the sweep settings.</summary>
    public async Task<RfExplorerConfig> ConfigureAsync(double startMHz, double endMHz, CancellationToken ct)
    {
        if (!IsOpen) throw new InvalidOperationException("RF Explorer is not connected.");

        var cfg = Config ?? await WaitForConfigAsync(_ => true, RequestConfig, TimeSpan.FromSeconds(5), ct);

        if (!Covers(cfg, startMHz, endMHz))
        {
            bool target = !cfg.ExpansionActive;
            cfg = await WaitForConfigAsync(c => c.ExpansionActive == target,
                () => SwitchModule(target), TimeSpan.FromSeconds(8), ct);
            if (!Covers(cfg, startMHz, endMHz))
                throw new InvalidOperationException(
                    $"Neither RF Explorer module covers {startMHz:0}–{endMHz:0} MHz.");
        }

        if (endMHz - startMHz > cfg.MaxSpanMHz + 0.5)
            throw new InvalidOperationException(
                $"Span {endMHz - startMHz:0} MHz exceeds the device maximum of {cfg.MaxSpanMHz:0} MHz.");

        return await WaitForConfigAsync(
            c => Math.Abs(c.StartMHz - startMHz) <= Math.Max(1, c.StepMHz * 2),
            () => SetSweep(startMHz, endMHz), TimeSpan.FromSeconds(8), ct);
    }

    /// <summary>Configures a range and collects a number of sweeps into a summary segment.</summary>
    public async Task<SpectrumSegment> CaptureSegmentAsync(double startMHz, double endMHz, int sweeps,
        CancellationToken ct)
    {
        var cfg = await ConfigureAsync(startMHz, endMHz, ct);
        var collected = new List<double[]>();
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        int skip = 1;   // the first sweep after a reconfigure can be partial

        void OnSweep(double[] data, RfExplorerConfig c)
        {
            if (Math.Abs(c.StartMHz - cfg.StartMHz) > 0.001) return;
            lock (collected)
            {
                if (skip-- > 0) return;
                collected.Add(data);
                if (collected.Count >= sweeps) done.TrySetResult();
            }
        }

        SweepReceived += OnSweep;
        try
        {
            await done.Task.WaitAsync(TimeSpan.FromSeconds(10 + sweeps * 3), ct);
        }
        catch (TimeoutException)
        {
            lock (collected)
                if (collected.Count < 3)
                    throw new TimeoutException("RF Explorer stopped sending sweeps.");
        }
        finally
        {
            SweepReceived -= OnSweep;
        }

        List<double[]> copy;
        lock (collected) copy = collected.ToList();
        return SpectrumAnalysis.BuildSegment(cfg, copy);
    }

    /// <summary>
    /// Parks on one 20 MHz channel and collects for a fixed time. Spans this narrow sweep
    /// fast, so the duty cycle it measures is a usable airtime figure.
    /// </summary>
    public async Task<SpectrumSegment> CaptureDwellAsync(double centerMHz, TimeSpan duration,
        CancellationToken ct, double? noiseFloor = null)
    {
        var cfg = await ConfigureAsync(centerMHz - 10, centerMHz + 10, ct);
        var collected = new List<double[]>();
        int skip = 1;

        void OnSweep(double[] data, RfExplorerConfig c)
        {
            if (Math.Abs(c.StartMHz - cfg.StartMHz) > 0.001) return;
            lock (collected)
            {
                if (skip-- > 0) return;
                collected.Add(data);
            }
        }

        SweepReceived += OnSweep;
        try
        {
            await Task.Delay(duration, ct);
        }
        finally
        {
            SweepReceived -= OnSweep;
        }

        List<double[]> copy;
        lock (collected) copy = collected.ToList();
        if (copy.Count == 0) throw new TimeoutException("No sweeps arrived during the dwell capture.");
        return SpectrumAnalysis.BuildSegment(cfg, copy, noiseFloor);
    }

    static bool Covers(RfExplorerConfig c, double start, double end) =>
        start >= c.MinMHz - 0.5 && end <= c.MaxMHz + 0.5;

    async Task<RfExplorerConfig> WaitForConfigAsync(Func<RfExplorerConfig, bool> match, Action send,
        TimeSpan timeout, CancellationToken ct)
    {
        var tcs = new TaskCompletionSource<RfExplorerConfig>(TaskCreationOptions.RunContinuationsAsynchronously);
        void On(RfExplorerConfig c) { if (match(c)) tcs.TrySetResult(c); }

        ConfigReceived += On;
        try
        {
            send();
            var deadline = DateTime.UtcNow + timeout;
            while (true)
            {
                try
                {
                    return await tcs.Task.WaitAsync(TimeSpan.FromMilliseconds(1500), ct);
                }
                catch (TimeoutException) when (DateTime.UtcNow < deadline)
                {
                    RequestConfig();   // nudge the device in case it didn't volunteer a config line
                }
            }
        }
        catch (TimeoutException)
        {
            throw new TimeoutException("RF Explorer did not confirm the new settings. Check the baud rate and cable.");
        }
        finally
        {
            ConfigReceived -= On;
        }
    }

    void SendText(string cmd) => Send(Encoding.ASCII.GetBytes(cmd));

    void Send(byte[] body)
    {
        var msg = new byte[body.Length + 2];
        msg[0] = (byte)'#';
        msg[1] = (byte)msg.Length;
        body.CopyTo(msg, 2);
        lock (_writeLock) _port.Write(msg, 0, msg.Length);
    }

    void ReadLoop()
    {
        try
        {
            while (_running)
            {
                int b = Next();
                if (b == '$')
                {
                    int type = Next();
                    int count = -1;
                    if (type == 'S') count = Next();
                    else if (type == 's') count = (Next() + 1) * 16;
                    if (count > 0) ReadSweep(count);
                }
                else if (b == '#')
                {
                    HandleText(ReadLine());
                }
            }
        }
        catch (Exception ex) when (ex is IOException or InvalidOperationException
                                       or OperationCanceledException or UnauthorizedAccessException)
        {
            if (_running)
            {
                _running = false;
                Disconnected?.Invoke(ex.Message);
            }
        }
    }

    int Next()
    {
        while (true)
        {
            if (!_running) throw new OperationCanceledException();
            try
            {
                int b = _port.ReadByte();
                if (b < 0) throw new IOException("Serial stream closed.");
                return b;
            }
            catch (TimeoutException) { /* keep waiting */ }
        }
    }

    void ReadSweep(int count)
    {
        var data = new double[count];
        for (int i = 0; i < count; i++) data[i] = -Next() / 2.0;
        var cfg = Config;
        if (cfg != null) SafeInvoke(() => SweepReceived?.Invoke(data, cfg));
    }

    // A throwing subscriber must not kill the reader thread (that would crash the app).
    static void SafeInvoke(Action a)
    {
        try { a(); }
        catch (Exception ex) { Debug.WriteLine("RF Explorer event handler failed: " + ex); }
    }

    string ReadLine()
    {
        var sb = new StringBuilder();
        while (sb.Length < 1024)
        {
            int b = Next();
            if (b == '\n') break;
            if (b != '\r') sb.Append((char)b);
        }
        return sb.ToString();
    }

    void HandleText(string line)
    {
        if (line.StartsWith("C2-F:", StringComparison.Ordinal))
        {
            // Start_Freq(kHz), Freq_Step(Hz), Amp_Top, Amp_Bottom, Sweep_Steps, ExpModuleActive,
            // CurrentMode, Min_Freq(kHz), Max_Freq(kHz), Max_Span(kHz), RBW(kHz), ...
            var f = line[5..].Split(',');
            double D(int i, double fallback) =>
                i < f.Length && double.TryParse(f[i].Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var v)
                    ? v : fallback;

            var cfg = new RfExplorerConfig(
                StartMHz: D(0, 0) / 1000.0,
                StepMHz: D(1, 0) / 1e6,
                AmpTop: (int)D(2, -10),
                AmpBottom: (int)D(3, -120),
                SweepPoints: (int)D(4, 112),
                ExpansionActive: D(5, 0) == 1,
                Mode: (int)D(6, 0),
                MinMHz: D(7, 0) / 1000.0,
                MaxMHz: D(8, 0) / 1000.0,
                MaxSpanMHz: D(9, 600_000) / 1000.0,
                RbwKHz: D(10, 600));
            Config = cfg;
            SafeInvoke(() => ConfigReceived?.Invoke(cfg));
        }
        else if (line.StartsWith("C2-M:", StringComparison.Ordinal))
        {
            ModelInfo = line[5..];
        }
    }

    public void Dispose()
    {
        _running = false;
        try { if (_port.IsOpen) _port.Close(); } catch { /* already gone */ }
        _reader?.Join(1000);
        _port.Dispose();
    }
}
