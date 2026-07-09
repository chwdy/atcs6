"""Chip register/bit logic, ported from the demo scripts (`i2c.py`,
`gpio.ipynb`).

Each class takes a duck-typed *device* (real `Ch347Device` or `FakeCh347Device`)
and its own configuration (I2C address, SPI select line, chain length). The chip
owns the register math and bit packing; the device owns the bus transactions and
the pad/polarity wiring.
"""
