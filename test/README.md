# Tests on the RTL

cocotb tests that run firmware on the RTL in lockstep with the cycle model
([lockstep.py](lockstep.py)): every pad and every instruction commit is
compared each cycle, and wire models from `tools/tempo/models/` check the
protocols on the pins.

```sh
make -B                                                  # all modules, 8 random seeds
make -B COCOTB_TEST_MODULES=test_random RAND_SEEDS=100   # more random programs
make -B COCOTB_TEST_MODULES=test COCOTB_TEST_FILTER=test_spi_mode0_jedec
```

Gate level: copy the hardened netlist to `gate_level_netlist.v`, set
`PDK_ROOT`, then `make -B GATES=yes`. This runs the subset named in the
[Makefile](Makefile) without a waveform dump. `../tools/gl_run.sh` does the
same in a scratch directory and can back-annotate an SDF file.
