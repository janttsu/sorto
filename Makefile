PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install test lint run doctor

help:
	@echo "make install  - create a virtualenv (.venv) and editable-install with dev extras"
	@echo "make test     - run pytest in .venv"
	@echo "make lint     - ruff check in .venv"
	@echo "make run      - sorto --help"
	@echo "make doctor   - sorto doctor"
	@echo "activate with:  source .venv/bin/activate"

venv:
	@test -x $(PY) || $(PYTHON) -m venv $(VENV)
	$(PIP) install -U pip

install: venv
	$(PIP) install -e ".[dev]"

test: install
	$(PY) -m pytest -q

lint: install
	$(VENV)/bin/ruff check src tests

run: install
	$(PY) -m sorto --help

doctor: install
	$(PY) -m sorto doctor
