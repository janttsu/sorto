PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: help venv install test lint run doctor compile

help:
	@echo "make install  - create a virtualenv (.venv) and editable-install with dev extras"
	@echo "make test     - run pytest in .venv"
	@echo "make lint     - ruff check in .venv"
	@echo "make run      - sorto --help"
	@echo "make doctor   - sorto doctor"
	@echo "make compile  - byte-compile and build sdist+wheel into dist/"
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

compile: install
	$(PY) -m compileall -q src/sorto
	$(PIP) install -q build
	$(PY) -m build
	@ls -l dist/sorto-*.whl dist/sorto-*.tar.gz
