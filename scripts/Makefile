# =============================================================================
# CylinderUI for Llama.cpp - Makefile
# -----------------------------------------------------------------------------
# Detects the OS and delegates to the right scripts. On Windows use the
# justfile or call install.ps1 directly (GNU make is uncommon there).
#
#   make install   full turn-key install + start
#   make run       start services
#   make stop       stop services
#   make status    show service/platform status
#   make update    re-run installer (idempotent: pulls/rebuilds/updates)
#   make bench     run rapido benchmark on the default model
#   make clean     stop services + remove venv/vendor/run/logs (keeps models)
# =============================================================================

SHELL := /bin/bash
UNAME_S := $(shell uname -s 2>/dev/null || echo Windows)
HERE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# Extra flags for install/bench, e.g.: make install ARGS="--cpu --no-model"
ARGS ?=
# Benchmark profile: make bench PROFILE=medio MODEL=path.gguf
PROFILE ?= rapido
MODEL ?=

.PHONY: install run stop status update bench clean help

help:
	@sed -n '1,20p' $(firstword $(MAKEFILE_LIST))

install:
	@bash "$(HERE)/install.sh" $(ARGS)

run:
	@bash "$(HERE)/scripts/run.sh"

stop:
	@bash "$(HERE)/scripts/stop.sh"

status:
	@bash "$(HERE)/scripts/status.sh"

update:
	@CYL_FORCE_REBUILD=1 bash "$(HERE)/install.sh" --dev $(ARGS)

bench:
	@bash "$(HERE)/scripts/bench.sh" $(MODEL) $(PROFILE)

clean: stop
	@echo "[cyl] removing .venv vendor run logs (models kept)"
	@rm -rf "$(HERE)/../.venv" "$(HERE)/../vendor" "$(HERE)/../run" "$(HERE)/../logs"
	@echo "[ok] clean done"
