.PHONY: help update

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

update:  ## Pull latest + reinstall (handles pyproject changes)
	git pull
	pip install -e ".[dev]"
