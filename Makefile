.PHONY: install baseline advanced evaluate test clean

install:
	python -m pip install -r requirements.txt
	python benchmarks/setup_benchmarks.py

baseline:
	python -m src.cli run-baseline --all

advanced:
	python -m src.cli run-advanced --all

evaluate:
	python src/evaluate.py

test:
	pytest -q

clean:
	rm -rf trajectories/baseline/*.json trajectories/advanced/*.json benchmarks/*/physical.db results/*.json results/comparison.md
