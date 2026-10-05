# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v tempo_core.v tempo_lane.v tempo_host.v

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
VERILOG_SOURCES += $(PWD)/sram/RM_IHPSG13_1P_1024x16_c2_bm_bist.v $(PWD)/sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v
COMPILE_ARGS    += -DFUNCTIONAL

else

# Gate level simulation:
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_io/verilog/sg13cmos5l_io.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v

# The gate-level run is a subset without waveform dump, so it fits a CI job:
# UART, SPI, I2C, USB-LS, PS/2, CAN, the bench-tool use cases 1-3 and both demos.
NODUMP ?= yes
# quoted: cocotb's makefile passes the value to the shell as is
COCOTB_TEST_FILTER ?= 'test_host_boot_and_uart_tx|test_uart_rx_fast_sender|test_spi_mode0_jedec|test_spi_mode3_flash_read|test_i2c_write_read_with_stretching|test_usb_ls_rx_and_tx|test_ps2_device_and_host|test_can_|test_uc1_uart_autobaud_console|test_uc2_i2c_bringup|test_uc3_spi_flash_programmer|test_demo_'

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v
VERILOG_SOURCES += $(PWD)/sram/RM_IHPSG13_1P_1024x16_c2_bm_bist.v $(PWD)/sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v

endif

# SDF=<file> back-annotates gate-level timing (with GATES=yes)
ifneq ($(SDF),)
COMPILE_ARGS    += -gspecify -ginterconnect -DSDF_FILE=\"$(SDF)\"
endif

# NODUMP=yes skips the FST dump (gate-level runs are much faster without it)
ifeq ($(NODUMP),yes)
COMPILE_ARGS    += -DNODUMP
endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES ?= test,test_usecases,test_ps2,test_can,test_blocking,test_random

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim
