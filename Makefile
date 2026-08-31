.PHONY: install baseline advanced evaluate test demo web clean

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

demo:
	python -m demo.generate_demo_data
	@echo "open demo/index.html in a browser -- no server needed"

web:
	uvicorn webapp.app:app --reload --port 8000

clean:
	rm -rf trajectories/baseline/*.json trajectories/advanced/*.json benchmarks/*/physical.db results/*.json results/comparison.md demo/data.js
