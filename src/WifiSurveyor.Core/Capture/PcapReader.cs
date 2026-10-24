using System.Buffers.Binary;

namespace WifiSurveyor.Capture;

public sealed record CapturedFrame(DateTime Time, byte[] Data, int LinkType);

/// <summary>
/// Reads classic pcap and pcapng files. Only the two Wi-Fi link types matter here:
/// 127 (802.11 with a Radiotap header) and 105 (bare 802.11).
/// </summary>
public static class PcapReader
{
    public const int LinkRadiotap = 127;
    public const int LinkDot11 = 105;

    public static List<CapturedFrame> Read(string path, int maxFrames = 500_000)
    {
        using var stream = File.OpenRead(path);
        var magic = new byte[4];
        if (stream.Read(magic, 0, 4) != 4) throw new InvalidDataException("File is too short to be a capture.");
        stream.Position = 0;

        uint value = BinaryPrimitives.ReadUInt32LittleEndian(magic);
        return value switch
        {
            0x0A0D0D0A => ReadPcapNg(stream, maxFrames),
            0xA1B2C3D4 or 0xD4C3B2A1 or 0xA1B23C4D or 0x4D3CB2A1 => ReadPcap(stream, maxFrames),
            _ => throw new InvalidDataException("Not a pcap or pcapng file.")
        };
    }

    static List<CapturedFrame> ReadPcap(FileStream stream, int maxFrames)
    {
        var header = ReadExactly(stream, 24);
        uint magic = BinaryPrimitives.ReadUInt32LittleEndian(header);
        bool bigEndian = magic is 0xD4C3B2A1 or 0x4D3CB2A1;
        bool nanoseconds = magic is 0xA1B23C4D or 0x4D3CB2A1;
        int linkType = (int)U32(header, 20, bigEndian);

        var frames = new List<CapturedFrame>();
        var record = new byte[16];
        while (frames.Count < maxFrames && stream.Read(record, 0, 16) == 16)
        {
            uint seconds = U32(record, 0, bigEndian);
            uint fraction = U32(record, 4, bigEndian);
            int captured = (int)U32(record, 8, bigEndian);
            if (captured <= 0 || captured > 65_535) break;

            var data = ReadExactly(stream, captured);
            frames.Add(new CapturedFrame(
                DateTimeOffset.FromUnixTimeSeconds(seconds).UtcDateTime
                    .AddTicks((long)(nanoseconds ? fraction / 100 : fraction * 10)),
                data, linkType));
        }
        return frames;
    }

    static List<CapturedFrame> ReadPcapNg(FileStream stream, int maxFrames)
    {
        var frames = new List<CapturedFrame>();
        var linkTypes = new List<int>();
        var resolutions = new List<int>();
        bool bigEndian = false;
        var head = new byte[8];

        while (frames.Count < maxFrames && stream.Read(head, 0, 8) == 8)
        {
            uint blockType = BinaryPrimitives.ReadUInt32LittleEndian(head);
            if (blockType == 0x0A0D0D0A)
            {
                // Section header: the byte-order magic tells us how to read the rest.
                var rest = ReadExactly(stream, 4);
                bigEndian = BinaryPrimitives.ReadUInt32LittleEndian(rest) != 0x1A2B3C4D;
                linkTypes.Clear();
                resolutions.Clear();
            }

            uint blockLength = U32(head, 4, bigEndian);
            if (blockLength < 12 || blockLength > 100_000_000) break;
            int bodyLength = (int)blockLength - 12;
            long bodyStart = stream.Position - (blockType == 0x0A0D0D0A ? 4 : 0);
            stream.Position = bodyStart;
            var body = ReadExactly(stream, bodyLength);
            ReadExactly(stream, 4);   // trailing block length

            switch (blockType)
            {
                case 0x00000001:   // interface description
                    linkTypes.Add(BinaryPrimitives.ReadUInt16LittleEndian(body.AsSpan(0, 2)));
                    resolutions.Add(ReadTimestampResolution(body, bigEndian));
                    break;
                case 0x00000006:   // enhanced packet
                {
                    if (body.Length < 20) break;
                    int interfaceId = (int)U32(body, 0, bigEndian);
                    ulong stamp = ((ulong)U32(body, 4, bigEndian) << 32) | U32(body, 8, bigEndian);
                    int captured = (int)U32(body, 12, bigEndian);
                    if (captured <= 0 || 20 + captured > body.Length) break;

                    var data = new byte[captured];
                    Array.Copy(body, 20, data, 0, captured);
                    int resolution = interfaceId < resolutions.Count ? resolutions[interfaceId] : 6;
                    double seconds = stamp / Math.Pow(10, resolution);
                    frames.Add(new CapturedFrame(
                        DateTimeOffset.FromUnixTimeMilliseconds((long)(seconds * 1000)).UtcDateTime,
                        data,
                        interfaceId < linkTypes.Count ? linkTypes[interfaceId] : LinkRadiotap));
                    break;
                }
            }
        }
        return frames;
    }

    /// <summary>if_tsresol option: decimal places in the timestamp, 6 (microseconds) by default.</summary>
    static int ReadTimestampResolution(byte[] body, bool bigEndian)
    {
        int pos = 8;
        while (pos + 4 <= body.Length)
        {
            int code = bigEndian
                ? BinaryPrimitives.ReadUInt16BigEndian(body.AsSpan(pos, 2))
                : BinaryPrimitives.ReadUInt16LittleEndian(body.AsSpan(pos, 2));
            int length = bigEndian
                ? BinaryPrimitives.ReadUInt16BigEndian(body.AsSpan(pos + 2, 2))
                : BinaryPrimitives.ReadUInt16LittleEndian(body.AsSpan(pos + 2, 2));
            if (code == 0) break;
            if (code == 9 && length >= 1 && pos + 4 < body.Length)
            {
                byte v = body[pos + 4];
                return (v & 0x80) != 0 ? 6 : v;   // powers of two are rare; fall back to microseconds
            }
            pos += 4 + (length + 3) / 4 * 4;
        }
        return 6;
    }

    static uint U32(byte[] data, int offset, bool bigEndian) => bigEndian
        ? BinaryPrimitives.ReadUInt32BigEndian(data.AsSpan(offset, 4))
        : BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(offset, 4));

    static byte[] ReadExactly(Stream stream, int count)
    {
        var buffer = new byte[count];
        int read = 0;
        while (read < count)
        {
            int n = stream.Read(buffer, read, count - read);
            if (n <= 0) throw new EndOfStreamException("The capture file ended unexpectedly.");
            read += n;
        }
        return buffer;
    }
}
