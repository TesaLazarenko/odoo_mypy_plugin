# odoo_scan_rs

Optional Rust extension of `odoo_typing`: the scan of an addon's Python files
(`odoo_typing.scanner.walker`), the references of a file, the one-file class scan and
the registry's text searches, giving the same results as the Python code (checked on
every file and addon of Odoo 17-20). `odoo_typing.scanner.native` uses it when it is
installed; `ODOO_TYPING_NATIVE=0` turns it off.

Build into the active virtualenv:

    maturin develop --release

Without a system C compiler, a linker can be given to cargo, e.g. `zig cc` from the
`ziglang` package: `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=<a script running
"python -m ziglang cc">`.
