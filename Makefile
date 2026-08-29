.PHONY: install baseline advanced evaluate test clean

install:
	pip install -r requirements.txt
	python benchmarks/setup_benchmarks.py

baseline:
	python src/cli.py run-baseline --all

advanced:
	python src/cli.py run-advanced --all

evaluate:
	python src/evaluate.py

test:
	pytest -q

clean:
	rm -rf trajectories/*.json benchmarks/*/physical.db
