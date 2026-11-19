.PHONY: install web test benchmark demo worker-image
PYTHON ?= python
install:
	$(PYTHON) -m pip install -c requirements.lock -e '.[test]'
web:
	npm --prefix web ci
	npm --prefix web run build
test:
	$(PYTHON) -m unittest discover -s tests -v
benchmark:
	$(PYTHON) -m scripts.benchmark --examples --output examples/benchmark.json
demo: web
	$(PYTHON) -m filetruth.cli serve
worker-image:
	docker build -t filetruth-worker:local .
