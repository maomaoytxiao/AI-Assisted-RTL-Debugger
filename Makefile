# AI-assisted DV prototype - milestone 1: sync FIFO with Icarus Verilog
#
#   make test   compile + run the self-checking testbench (fails on error)
#   make wave   run the test, then open the VCD in GTKWave (if a display exists)
#   make lint   optional lint of the RTL with the installed Verilator 4.028
#   make db     convert the VCD from 'make test' into build/waves.db (SQLite)
#   make db-test run the waveform database / query tool tests
#   make fault-test  simulate the injected-fault RTL variants (no LLM needed)
#   make debug-data  build agent-visible debug bundles in build/debug (no LLM)
#   make eval   score saved AI diagnosis reports (reports/*.json) against
#               the ground truth in faults/faults.json (no LLM needed)
#   make clean  remove build outputs

IVERILOG ?= iverilog
VVP      ?= vvp
GTKWAVE  ?= gtkwave
VERILATOR ?= verilator
PYTHON   ?= python3

BUILD := build
RTL   := rtl/sync_fifo.sv
TB    := tb/tb_sync_fifo.sv
SIM   := $(BUILD)/tb_sync_fifo.vvp
VCD   := $(BUILD)/sync_fifo.vcd
LOG   := $(BUILD)/sim.log
GTKW  := waves/sync_fifo.gtkw
DB    := $(BUILD)/waves.db

IVFLAGS := -g2012 -Wall -s tb_sync_fifo -DVCD_FILE=\"$(VCD)\"

.PHONY: test wave lint db db-test fault-test debug-data eval clean

$(BUILD):
	mkdir -p $(BUILD)

$(SIM): $(RTL) $(TB) | $(BUILD)
	$(IVERILOG) $(IVFLAGS) -o $@ $(RTL) $(TB)

test: $(SIM)
	$(VVP) -n $(SIM) | tee $(LOG)
	@grep -q '^TEST PASSED' $(LOG) || { echo "make test: FAILED (see $(LOG))"; exit 1; }
	@echo "make test: PASSED  (log: $(LOG), waves: $(VCD))"

wave: test
	@if command -v $(GTKWAVE) >/dev/null 2>&1 && [ -n "$$DISPLAY" ]; then \
		$(GTKWAVE) $(VCD) $(GTKW) >/dev/null 2>&1 & \
	else \
		echo "No display/GTKWave available; open $(VCD) manually (layout: $(GTKW))"; \
	fi

lint:
	$(VERILATOR) --lint-only -Wall $(RTL)

db:
	@test -f $(VCD) || { echo "make db: $(VCD) not found; run 'make test' first"; exit 1; }
	$(PYTHON) scripts/vcd_to_sqlite.py $(VCD) $(DB)

db-test:
	@test -f $(DB) || { echo "make db-test: $(DB) not found; run 'make db' first"; exit 1; }
	$(PYTHON) -m unittest discover -s tests -v

fault-test:
	$(PYTHON) scripts/fault_campaign.py run

debug-data: fault-test
	$(PYTHON) scripts/fault_campaign.py bundle

eval: debug-data
	$(PYTHON) scripts/evaluate_diagnosis.py

clean:
	rm -rf $(BUILD)
