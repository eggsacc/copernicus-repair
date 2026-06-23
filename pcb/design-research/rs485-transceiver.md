# UART to RS485 transceiver

**TL;DR:**
>Comparison of various RS485 transceivers.
>**Final choice: THVD1400**

**References:**
>- [SN65HVD485E datasheet](https://www.ti.com/lit/ds/symlink/sn65hvd485e.pdf?ts=1782191083756)
>- [MAX485 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX1487-MAX491.pdf)
>- [MAX3485 datasheet](https://www.analog.com/media/cn/technical-documentation/data-sheets/1079.pdf)
>- [THVD1450 datasheet](https://www.ti.com/lit/ds/symlink/thvd1450.pdf)
>- [SP3485 datasheet](https://docs.rs-online.com/1956/0900766b81141d5c.pdf)

| IC          | EOL | Voltage | Others                                                             | Price |
|-------------|-----|---------|--------------------------------------------------------------------|-------|
| MAX485      | Yes | 5V      | Most common transceiver                                            | $0.28 |
| MAX3485     | Yes | 3.3V    | 3.3V version of MAX485                                             | $0.38 |
| SP3485      | Yes | 3.3V    | Cheapest, legacy model                                             | $0.30 |
| THVD1400    | No  | 3.3-5V  | +-12kv ESD protection<br>500kbps                                   | $0.32 |
| THVD1450    | No  | 3.3-5V  | +-30kv ESD protection<br>50Mbps high speed data<br>500kbps low EMI | $0.73 |
| SN65HVD485E | No  | 5V      | +-15kv ESD protection<br>10Mbps                                    | $0.44 |

## Selection

Note that the max configurable baud rate of the servo drievrs are only 115200bps. Hence, high-speed transceivers are do not give any advantages in the system; in fact, slower transceivers provide a cleaner signal with lesser EMI.

"Slower" transceivers like the `THVD1400` (compared to `THVD1450`) are slew-rate limited, meaning they shape the signal edges deliberately to be more trapezoidal with a longer rise/fall time delta. In comparison, high-speed transceivers have sharper edges to minimize transition times which leads to higher EMI.

Final selection: `THVD1400`. More recent, cost competitive, ESD protection, 3.3V tolerant, moderate speed for application.