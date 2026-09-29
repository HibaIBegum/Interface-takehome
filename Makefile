PY ?= .venv/bin/python

.PHONY: demo test serve-mock

demo:  ## all scenarios end to end (needs ANTHROPIC_API_KEY, ANTHROPIC_MODEL); evidence -> evidence/
	PYTHON=$(PY) scripts/demo.sh

test:
	$(PY) -m pytest -q

serve-mock:
	$(PY) -m cua.cli serve-mock
