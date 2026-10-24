using WifiSurveyor.Models;

namespace WifiSurveyor.Capture;

public static class CaptureAnalyzer
{
    /// <summary>
    /// Builds a summary from captured frames. Airtime here is measured rather than sampled:
    /// every frame's duration is added up, so the total is what the channel was really doing.
    /// </summary>
    public static CaptureSummary Analyze(IReadOnlyList<CapturedFrame> frames, string source)
    {
        var summary = new CaptureSummary { Source = source, Frames = frames.Count };
        if (frames.Count == 0) return summary;

        summary.Start = frames[0].Time.ToLocalTime();
        double seconds = (frames[^1].Time - frames[0].Time).TotalSeconds;
        summary.Seconds = seconds > 0.05 ? seconds : 0.05;

        var stats = new Dictionary<string, BssAccumulator>(StringComparer.OrdinalIgnoreCase);
        var frequencies = new Dictionary<int, int>();
        double totalAirtimeUs = 0;
        int ackable = 0;

        foreach (var frame in frames)
        {
            int offset = 0;
            RadiotapInfo radio = default;
            if (frame.LinkType == PcapReader.LinkRadiotap)
            {
                radio = Radiotap.Parse(frame.Data, 0, frame.Data.Length);
                if (radio.HeaderLength <= 0 || radio.HeaderLength >= frame.Data.Length) continue;
                if (radio.BadFcs)
                {
                    summary.BadFcs++;
                    continue;   // a corrupt frame tells us nothing reliable
                }
                offset = radio.HeaderLength;
                if (radio.FreqMHz is int f) frequencies[f] = frequencies.GetValueOrDefault(f) + 1;
            }

            int length = frame.Data.Length - offset;
            if (Dot11.Parse(frame.Data, offset, length) is not { } dot11) continue;

            bool unicast = !Dot11.IsGroupAddress(dot11.Addr1);
            bool expectsAck = unicast && dot11.Type != 1;
            if (expectsAck) ackable++;

            double airtime = Dot11.AirtimeUs(length, radio.RateMbps, radio.IsDsss, expectsAck);
            totalAirtimeUs += airtime;

            if (dot11.IsControl) summary.Control++;
            else if (dot11.IsData) summary.Data++;
            else summary.Management++;
            if (dot11.IsDeauth) summary.Deauths++;
            if (dot11.IsDisassoc) summary.Disassocs++;

            bool countsForRetry = dot11.Type != 1;
            if (countsForRetry)
            {
                summary.RetryEligible++;
                if (dot11.Retry) summary.Retries++;
            }

            var bssid = dot11.Bssid;
            if (bssid == null || Dot11.IsGroupAddress(bssid)) continue;
            if (!stats.TryGetValue(bssid, out var acc)) stats[bssid] = acc = new BssAccumulator();

            acc.Frames++;
            acc.AirtimeUs += airtime;
            if (countsForRetry)
            {
                acc.RetryEligible++;
                if (dot11.Retry) acc.Retries++;
            }
            if (radio.SignalDbm is int signal && dot11.Addr2 == bssid)
            {
                acc.SignalSum += signal;
                acc.SignalCount++;
            }

            foreach (var address in new[] { dot11.Addr1, dot11.Addr2 })
                if (address != null && !Dot11.IsGroupAddress(address) &&
                    !address.Equals(bssid, StringComparison.OrdinalIgnoreCase))
                    acc.Clients.Add(address);

            if ((dot11.IsBeacon || dot11.IsProbeResponse) && acc.Beacon == null)
            {
                // Fixed fields: timestamp, beacon interval, capability information
                int ieStart = offset + dot11.HeaderLength + 12;
                if (ieStart < frame.Data.Length)
                    acc.Beacon = InformationElements.Parse(frame.Data, ieStart, frame.Data.Length);
                if (dot11.IsBeacon) acc.Beacons++;
            }
            else if (dot11.IsBeacon)
            {
                acc.Beacons++;
            }
        }

        summary.AckFrames = ackable;
        summary.AirtimeFraction = Math.Min(1, totalAirtimeUs / (summary.Seconds * 1_000_000));
        summary.RetryRate = summary.RetryEligible > 0 ? (double)summary.Retries / summary.RetryEligible : 0;
        summary.Clients = stats.Values.SelectMany(a => a.Clients).Distinct(StringComparer.OrdinalIgnoreCase).Count();

        if (frequencies.Count > 0)
        {
            summary.CenterMHz = frequencies.MaxBy(kv => kv.Value).Key;
            summary.Channel = Wifi.WifiChannels.All
                .FirstOrDefault(c => Math.Abs(c.CenterMHz - summary.CenterMHz.Value) < 1)?.Number ?? 0;
        }

        summary.Bss = stats
            .OrderByDescending(kv => kv.Value.AirtimeUs)
            .Take(40)
            .Select(kv => new BssStat
            {
                Bssid = kv.Key,
                Ssid = kv.Value.Beacon?.Ssid ?? "",
                Channel = kv.Value.Beacon?.Channel ?? 0,
                WidthMHz = kv.Value.Beacon?.WidthMHz ?? 0,
                Streams = kv.Value.Beacon?.Streams,
                Generation = kv.Value.Beacon?.Generation ?? "",
                Security = kv.Value.Beacon?.Security ?? "",
                ProtectedManagementFrames = kv.Value.Beacon?.ProtectedManagementFrames,
                ReportedUtilization = kv.Value.Beacon?.Utilization,
                ReportedStations = kv.Value.Beacon?.Stations,
                SupportsFastRoaming = kv.Value.Beacon?.SupportsFastRoaming ?? false,
                SupportsRrm = kv.Value.Beacon?.SupportsRrm ?? false,
                Frames = kv.Value.Frames,
                Retries = kv.Value.Retries,
                RetryRate = kv.Value.RetryEligible > 0 ? (double)kv.Value.Retries / kv.Value.RetryEligible : 0,
                AirtimeFraction = Math.Min(1, kv.Value.AirtimeUs / (summary.Seconds * 1_000_000)),
                Clients = kv.Value.Clients.Count,
                AvgRssi = kv.Value.SignalCount > 0 ? kv.Value.SignalSum / (double)kv.Value.SignalCount : null
            })
            .ToList();

        return summary;
    }

    sealed class BssAccumulator
    {
        public int Frames, Retries, RetryEligible, SignalCount, SignalSum, Beacons;
        public double AirtimeUs;
        public BeaconInfo? Beacon;
        public HashSet<string> Clients { get; } = new(StringComparer.OrdinalIgnoreCase);
    }
}
