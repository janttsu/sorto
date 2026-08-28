.PHONY: help install test lint run doctor

help:
	@echo "make install  - editable install with dev extras"
	@echo "make test     - run pytest"
	@echo "make lint     - ruff check"
	@echo "make run      - sorto --help"
	@echo "make doctor   - sorto doctor"

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	python3 -m ruff check src tests

run:
	python3 -m sorto --help

doctor:
	python3 -m sorto doctor
