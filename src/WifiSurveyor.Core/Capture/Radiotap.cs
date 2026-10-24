using System.Buffers.Binary;

namespace WifiSurveyor.Capture;

public readonly record struct RadiotapInfo(
    int HeaderLength,
    int? FreqMHz,
    int? SignalDbm,
    int? NoiseDbm,
    double? RateMbps,
    bool BadFcs,
    bool FcsAtEnd,
    bool IsDsss);

/// <summary>
/// Parses the Radiotap header that monitor-mode drivers prepend to each frame.
/// Fields appear in bit order, each aligned to its own size, so the whole table has to be
/// walked even for the few values we want.
/// </summary>
public static class Radiotap
{
    // (alignment, size) per present-bit, in bit order
    static readonly (int Align, int Size)[] Fields =
    {
        (8, 8),   // 0  TSFT
        (1, 1),   // 1  flags
        (1, 1),   // 2  rate, in 500 kbps units
        (2, 4),   // 3  channel: frequency + flags
        (2, 2),   // 4  FHSS
        (1, 1),   // 5  antenna signal, dBm
        (1, 1),   // 6  antenna noise, dBm
        (2, 2),   // 7  lock quality
        (2, 2),   // 8  TX attenuation
        (2, 2),   // 9  dB TX attenuation
        (1, 1),   // 10 TX power, dBm
        (1, 1),   // 11 antenna index
        (1, 1),   // 12 antenna signal, dB
        (1, 1),   // 13 antenna noise, dB
        (2, 2),   // 14 RX flags
        (2, 2),   // 15 TX flags
        (1, 1),   // 16 RTS retries
        (1, 1),   // 17 data retries
        (4, 8),   // 18 XChannel
        (1, 3),   // 19 MCS
        (4, 8),   // 20 A-MPDU status
        (2, 12),  // 21 VHT
        (8, 12),  // 22 timestamp
        (2, 12),  // 23 HE
        (2, 12),  // 24 HE-MU
        (2, 6),   // 25 HE-MU other user
        (1, 1),   // 26 zero-length PSDU
        (2, 4)    // 27 L-SIG
    };

    // HT/VHT rates: 20 MHz, one spatial stream, 800 ns guard interval
    static readonly double[] BaseRates = { 6.5, 13, 19.5, 26, 39, 52, 58.5, 65, 78, 86.7 };

    public static RadiotapInfo Parse(byte[] data, int offset, int length)
    {
        if (length < 8) return new RadiotapInfo(0, null, null, null, null, false, false, false);

        int headerLength = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 2, 2));
        if (headerLength < 8 || headerLength > length)
            return new RadiotapInfo(0, null, null, null, null, false, false, false);

        // Present bitmaps: the top bit of each means another word follows.
        var present = new List<uint>();
        int pos = offset + 4;
        while (pos + 4 <= offset + headerLength)
        {
            uint word = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(pos, 4));
            present.Add(word);
            pos += 4;
            if ((word & 0x8000_0000) == 0) break;
        }

        int? freq = null, signal = null, noise = null;
        double? rate = null;
        bool badFcs = false, fcsAtEnd = false, isDsss = false;
        int end = offset + headerLength;

        for (int bit = 0; bit < Fields.Length; bit++)
        {
            if (bit >= present.Count * 32) break;
            if ((present[bit / 32] & (1u << (bit % 32))) == 0) continue;

            var (align, size) = Fields[bit];
            pos = Align(pos - offset, align) + offset;
            if (pos + size > end) break;

            switch (bit)
            {
                case 1:
                    byte flags = data[pos];
                    fcsAtEnd = (flags & 0x10) != 0;
                    badFcs = (flags & 0x40) != 0;
                    break;
                case 2:
                    rate = data[pos] * 0.5;
                    isDsss = rate <= 11;
                    break;
                case 3:
                    freq = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(pos, 2));
                    break;
                case 5:
                    signal = (sbyte)data[pos];
                    break;
                case 6:
                    noise = (sbyte)data[pos];
                    break;
                case 19:
                {
                    byte known = data[pos], mcsFlags = data[pos + 1], index = data[pos + 2];
                    int width = (known & 0x02) != 0 ? (mcsFlags & 0x03) switch { 1 => 40, _ => 20 } : 20;
                    bool shortGi = (known & 0x04) != 0 && (mcsFlags & 0x04) != 0;
                    rate = HtRate(index % 8, 1 + index / 8, width, shortGi) ?? rate;
                    isDsss = false;
                    break;
                }
                case 21:
                {
                    byte bandwidth = data[pos + 3];
                    byte mcsNss = data[pos + 4];
                    int nss = mcsNss & 0x0F, index = (mcsNss >> 4) & 0x0F;
                    int width = bandwidth switch { >= 8 and < 12 => 160, >= 4 and < 8 => 80, >= 1 and < 4 => 40, _ => 20 };
                    bool shortGi = (data[pos + 2] & 0x04) != 0;
                    if (nss > 0) rate = HtRate(index, nss, width, shortGi) ?? rate;
                    isDsss = false;
                    break;
                }
            }
            pos += size;
        }
        return new RadiotapInfo(headerLength, freq, signal, noise, rate, badFcs, fcsAtEnd, isDsss);
    }

    static int Align(int value, int align) => (value + align - 1) / align * align;

    static double? HtRate(int mcsIndex, int streams, int widthMHz, bool shortGi)
    {
        if (mcsIndex < 0 || mcsIndex >= BaseRates.Length || streams < 1) return null;
        double width = widthMHz switch { 160 => 9.0, 80 => 4.5, 40 => 2.077, _ => 1 };
        return BaseRates[mcsIndex] * width * streams * (shortGi ? 1.111 : 1);
    }
}
