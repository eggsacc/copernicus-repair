# Voltage step down for driving relays

The relays (DG85B-7011-75-1024) used have a internal coil resistance of $330m\Omega$ as suggested by the datasheet.

At full charge, the battery provides about 29.3V. If this voltage is directly used to drive the relays, the power loss is:

$$
\begin{align}
P_{\text{coil loss}}&=\frac{V^2}{R} \\[10pt]
&=\frac{29.3^2}{330} \\[10pt]
&\approx 2.6W
\end{align}
$$

Multiplied by 2 relays in the circuit, $\approx 5.2W$ power loss just standing idle.

The must-operate voltage suggested by the datasheet is only 14.3V. Controlling the relays with the full battery voltage is just wasting power. Hence, a buck converter can be used to step down the battery input to about 20V to reduce losses and still control the relays with margin.

At 20V, the coil losses are:

$$
\begin{align}
P_{\text{coil loss}}&=\frac{20^2}{330} \\[10pt]
&\approx 1.2W
\end{align}
$$

Which is $\approx 53.8\%$ less power.

## Buck converter

The required current flowing through the coil of both relays is:

$$
\begin{align}
I_{\text{coils}} &= 2\cdot\frac{V_{sw}}{R_{\text{coil}}} \\[10pt]
&=2\cdot \frac{20}{330} \\[10pt]
&\approx 0.12A
\end{align}
$$

The same **LMR51610XDBVR** buck converter as the one used for the 3.3V rail is used since its current capacity (1A) is well above required. 

## Duty limit

Buck converters have a duty cycle limit since the output transistor cannot stay permenantly on for 100% duty as brief power-offs are required to charge the bootstrap capacitor. For example, a buck can never produce 24V out given 24V input. A 98% duty buck can produce a maximum of $0.98\times 24=23.52V$.

## Voltage dropout

When the supply voltage is less than the set output voltage, the converter enters the voltage dropout phase where the input voltage just passes though to the output with some losses.

## Voltage configuration

![alt text](assets/buck-an.png)

The datasheet does not provide standard values for 20V output, requiring manual calculations.

The resistors $R_{\text{FBT}}, R_{\text{FBB}}$ sets the output voltage. The equation is:

$$
R_{\text{FBT}} = \frac{(V_{\text{out}} - V_{\text{ref}})}{V_{\text{ref}}} \cdot R_{\text{FBB}}
$$

The datasheet recommends setting $R_{\text{FBB}}=22.1k\Omega$. Hence, $R_{\text{FBT}}$ can be calculated to be:

$$
\begin{align}
R_{\text{FBT}} &= \frac{20-0.8}{0.8} \cdot 22.1k \\[10pt]
&= 530.4k\Omega \\
&\approx 536k\Omega \: (\text{closest exact-value})
\end{align}
$$

## Inductor sizing

The minimum value of the output inductor is calculated by:

$$
L_{\text{min}} = \frac{V_{\text{In-max}} - V_{\text{out}}}{I_{\text{out}} \times K_{\text{Ind}}} \times \frac{V_{\text{out}}}{V_{\text{In-max}} \times f_{\text{sw}}}
$$

Assuming $V_{\text{In-max}}=30V,\: V_{\text{out}}=20V, \:I_{\text{out}}=1A,\: K_{\text{Ind}}=0.3,\: f_{\text{sw}}=400\text{kHz}$:

$$
\begin{align}
L_{\text{min}} &= \frac{30-20}{1\times 0.3} \times \frac{20}{30\times 400\times 10^3} \\[10pt]
&= 55.56\mu H \\
&\approx 56\mu H
\end{align}
$$

Taking $L=56\mu H$, the current ripple $\Delta i_L$ is:

$$
\begin{align}
\Delta i_L &= \frac{V_{\text{out}}\cdot (V_{\text{In-max}} - V_{\text{out}})}{V_{\text{In-max}}\cdot L \cdot f_{\text{SW}}} \\[10pt]
&= \frac{20\cdot (30-20)}{30\cdot 56\times 10^{-6}\cdot 400\times 10^3} \\[10pt]
&\approx 0.30A
\end{align}
$$

Peak inductor current: $1+0.30/2=1.15A$. This is below the converter's high-side peak current limit of 1.25A albeit a small margin.

Hence, a inductor must have:
- $>56\mu H$ Inductance
- $>1.5A$ RMS
- $>2.5A$ Saturation
- $\le 150m\Omega$ DCR

## Output capacitor sizing

>Quoting datasheet:
>The output capacitor or capacitors, COUT, must be chosen with care because it directly affects the steady state output voltage ripple, loop stability, and output voltage overshoot and undershoot during load current transient.

The output voltage ripple is essentially composed of two parts:

1) Inductor ripple current flowing through the Equivalent Series Resistance (ESR) of the output capacitors,

$$
\Delta V_{\mathrm{out-esr}} = \Delta I_{\mathrm{l}} \times \mathrm{ESR} = K_{\mathrm{ind}} \times I_{\mathrm{out}} \times \mathrm{ESR}
$$

2) Inductor current ripple charging and discharging the output capacitors.

$$
\Delta V_{\mathrm{out-c}} = \frac{\Delta i}{8 \times f_{\mathrm{sw}} \times C_{\mathrm{out}}} = \frac{K_{\mathrm{ind}} \times I_{\mathrm{out}}}{8 \times f_{\mathrm{sw}} \times C_{\mathrm{out}}}
$$

The control loop of the converter usually requires eight or more clock cycles to regulate the inductor current equal to the new load level during a load transient. The output capacitance must be large enough to supply the current difference to maintain the output voltage within the specified range. The minimum output capacitance needed is given by:

$$
C_{\mathrm{out}} > \frac{1}{2} \times \frac{6 \times \left(I_{\mathrm{oh}} - I_{\mathrm{ol}}\right)}{f_{\mathrm{sw}} \times \Delta V_{\mathrm{out-shoot}}}
$$

Following TI's design targets of $$\Delta V_{\mathrm{out-esr}}=\Delta V_{\mathrm{out-c}}=15mV$ and a ripple current of $0.3A$ from inductor selection earlier,

$$
\begin{align}
\mathrm{ESR} &= \frac{\Delta V_{\mathrm{out-esr}}}{K_{\mathrm{ind}} \times I_{\mathrm{out}}} \\[10pt]
&=\frac{15\times 10^{-3}}{0.3\times 1} \\[10pt]
&= 50\text{m}\Omega
\end{align}
$$

Following TI's design targets of $250mV$ transient limit:

$$
\begin{align}
C_{\mathrm{out}} &> \frac{1}{2} \times \frac{6 \times \left(I_{\mathrm{oh}} - I_{\mathrm{ol}}\right)}{f_{\mathrm{sw}} \times \Delta V_{\mathrm{out-shoot}}} \\[10pt]
&> \frac{3 \times 1}{400\times 10^3\times 250 \times 10^{-3}} \\[10pt]
&> 30\mu F
\end{align} 
$$

## Capacitor DC bias

Check manufacturer's DC bias curve! A capacitor rated at $30\mu F$ might only behave close to $15\mu F$ at specific voltages. Confirm with actual part datasheet and stack capacitors for better ESR, ESL and transient response.




