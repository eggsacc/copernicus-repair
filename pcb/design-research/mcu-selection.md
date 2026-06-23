# STM32 MCU selection

**TL;DR:**
>Presents control hardware requirement, comparison of different STM32 MCU candidates.
>**Final chosen MCU:** STM32F303CBT6

**References:**
>- [JLC parts library](https://jlcpcb.com/parts/)
>- [STM32 USART presentation](https://www.st.com/resource/en/product_training/STM32F7_Peripheral_USART.pdf)
>- [STM32 naming convention](https://www.digikey.com/en/maker/tutorials/2020/understanding-stm32-naming-conventions)
>- [STM32F4 series MCU](https://www.st.com/en/microcontrollers-microprocessors/stm32f4-series.html)

## Core functionality requirements

| Requirement  | Purpose                                                              |
|--------------|----------------------------------------------------------------------|
| USB-FS                             | Communication with SBC/PC                                            |
| DMA channels                       | Offload constant, heavy data feedback for ROS2 integration           |
| ADC                                | Thermistor/current sense readings                                    |
| USART w/RS485 hardware flow control| Motor driver command, connected to serial-RS485 converter            |
| GPIOs                              | - Status LEDs<br>- Soft-start MOSFET switching<br>- ESTOP monitoring |
| FPU                                | Heavy floating point data processing                                 |

## MCU selection

| MCU           | Clock  | FPU | USART hw. fc. | SRAM  | USB | Price  |
|---------------|--------|-----|---------------|-------|-----|--------|
| STM32F301C8T6 | 72MHz  | ✅   | ✅             | 16kb  | ❌   | $7.93  |
| STM32F303RBT6 | 72MHz  | ✅   | ✅             | 40kb  | ✅   | $2.98  |
| STM32F303CBT6 | 72MHz  | ✅   | ✅             | 40kb  | ✅   | $3.81  |
| STM32F411RCT6 | 100MHz | ✅   | ❌             | 128kb | ✅   | $4.93  |
| STM32F405R8T6 | 168MHz | ✅   | ❌             | 192kb | ✅   | $13.15 |

**Final chosen:** STM32F303CBT6

**Justification:**
- Sufficient SRAM for double-buffered ROS2 dataframe
- Newer USART peripheral, supports RS485 hardware flow control
- Native USB-FS
- Less pins than RBT6 cousin, smaller footprint (don't need so many pins)
- Readily available in JLCPCB parts catalogue
- Built-in comparators & ultra-fast ADCs



