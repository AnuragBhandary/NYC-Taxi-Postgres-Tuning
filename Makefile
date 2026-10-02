.PHONY: install data up down load-pg bench-pg mysql compare test lint typecheck check

install:
	uv sync

data:             ## 2023 yellow-taxi trips (12 x ~50 MB Parquet) + zone lookup, from the TLC CDN
	mkdir -p data
	for m in 01 02 03 04 05 06 07 08 09 10 11 12; do \
		curl -sSf -o data/yellow_tripdata_2023-$$m.parquet \
			https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-$$m.parquet; done
	curl -sSf -o data/taxi_zone_lookup.csv https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv

up:
	docker compose up -d --wait postgres

down:
	docker compose --profile mysql down

load-pg: up       ## baseline (trips_heap) and tuned (trips + indexes + MVs)
	uv run taxibench load-pg

bench-pg:
	uv run taxibench bench --design pg-baseline
	uv run taxibench bench --design pg-tuned
	uv run taxibench refresh

mysql:            ## laptop disk: free the baseline first, then load and benchmark MySQL 8.4
	uv run taxibench drop-baseline
	docker compose --profile mysql up -d --wait mysql
	uv run taxibench load-mysql
	uv run taxibench bench --design mysql

compare:
	uv run taxibench compare

test:
	docker compose --profile mysql up -d --wait
	uv run pytest --cov --cov-fail-under=90

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy

check: lint typecheck test
