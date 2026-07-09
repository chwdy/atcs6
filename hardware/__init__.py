"""Real hardware drivers for the operation runtime (design_new.md "Power").

This package is the concrete side of the hardware interface boundary. Nothing
above the interfaces (`OutputInterface`, `SwitchOutputInterface`,
`InputInterface`) imports from here except `hardware_config.build_session_interfaces`,
which constructs the real interfaces when a session is built in real mode.

Only `ch347_device` imports `ch347api` (the USB-HID driver). The chip classes,
interface adapters, and self-test operate on a duck-typed *device* object, so
they run against either the real `Ch347Device` or the in-memory `FakeCh347Device`
without a USB device attached — that is how the suite tests the whole stack.
"""
