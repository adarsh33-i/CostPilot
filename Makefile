.PHONY: install test lint demo serve check verify clean

PYTHON := /Users/adarshahebbar/Documents/CostPilot/.venv/bin/python
PROJECT := /Users/adarshahebbar/Documents/CostPilot

install:
	uv pip install -e "$(PROJECT)[dev]" --python $(PYTHON)

lint:
	$(PYTHON) -m ruff check $(PROJECT)

test:
	$(PYTHON) -m pytest $(PROJECT)/tests

demo:
	$(PYTHON) -m costpilot.cli demo

check:
	$(PYTHON) -m costpilot.cli check

verify:
	$(PYTHON) -m costpilot.cli verify

serve:
	$(PYTHON) -m costpilot.cli serve --port 8000

clean:
	rm -rf $(PROJECT)/.pytest_cache $(PROJECT)/.ruff_cache $(PROJECT)/data
