# Power architecture

>- [How to select surge diode](https://www.ti.com/lit/an/slvae37/slvae37.pdf?ts=1782528231635)
>- [Application of Relay Coil Suppression with DC Relays](https://www.te.com/en/products/relays-and-contactors/electromechanical-relays/intersection/relay-coil-suppression-dc-relays.html?utm_source=chatgpt.com&tab=pgp-story)
## Relay flyback diode 

A flyback diode is placed across the coils of the relays. The electromagnetic core of thre relay acts like a inductor and stores energy in the form of the magnetic field when powered. When turning the relay off, the magnetic field collapses and induces a reverse current that has to go somewhere.

A flyback diode is used to re-circulate and dissipate this induced current and protect other components around it. 