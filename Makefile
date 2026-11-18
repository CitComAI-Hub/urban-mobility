# Makefile for SUMO KServe Application

# Variables
IMAGE_NAME ?= sumo-kserve
IMAGE_TAG ?= latest
REGISTRY ?= ngomariz
FULL_IMAGE = $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
CONTAINER_NAME = sumo-kserve-container
PORT ?= 8080

# Environment variables for local deployment
KAFKA_BOOTSTRAP_SERVERS ?= kafka:29092
MINIO_ENDPOINT ?= minio:9000
MINIO_ACCESS_KEY ?= minioadmin
MINIO_SECRET_KEY ?= minioadmin
REDIS_HOST ?= redis
REDIS_PORT ?= 6379

.PHONY: help
help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

.PHONY: init
init: ## Initialize git submodules (SUMO)
	@echo "Initializing submodules..."
	git submodule update --init --recursive

.PHONY: build
build: ## Build the Docker image
	@echo "Building Docker image: $(FULL_IMAGE)"
	docker build -t $(FULL_IMAGE) .
	@echo "Build complete!"

.PHONY: build-no-cache
build-no-cache: ## Build the Docker image without cache
	@echo "Building Docker image without cache: $(FULL_IMAGE)"
	docker build --no-cache -t $(FULL_IMAGE) .
	@echo "Build complete!"

.PHONY: run
run: ## Run the container locally
	@echo "Starting container: $(CONTAINER_NAME)"
	docker run -d \
		--name $(CONTAINER_NAME) \
		-p $(PORT):8080 \
		-e KAFKA_BOOTSTRAP_SERVERS=$(KAFKA_BOOTSTRAP_SERVERS) \
		-e MINIO_ENDPOINT=$(MINIO_ENDPOINT) \
		-e MINIO_ACCESS_KEY=$(MINIO_ACCESS_KEY) \
		-e MINIO_SECRET_KEY=$(MINIO_SECRET_KEY) \
		-e MINIO_SECURE=false \
		-e REDIS_HOST=$(REDIS_HOST) \
		-e REDIS_PORT=$(REDIS_PORT) \
		-e MODEL_NAME=SUMO \
		$(FULL_IMAGE)
	@echo "Container started on port $(PORT)"

.PHONY: run-it
run-it: ## Run the container interactively
	@echo "Starting container interactively..."
	docker run -it --rm \
		--name $(CONTAINER_NAME) \
		-p $(PORT):8080 \
		-e KAFKA_BOOTSTRAP_SERVERS=$(KAFKA_BOOTSTRAP_SERVERS) \
		-e MINIO_ENDPOINT=$(MINIO_ENDPOINT) \
		-e MINIO_ACCESS_KEY=$(MINIO_ACCESS_KEY) \
		-e MINIO_SECRET_KEY=$(MINIO_SECRET_KEY) \
		-e MINIO_SECURE=false \
		-e REDIS_HOST=$(REDIS_HOST) \
		-e REDIS_PORT=$(REDIS_PORT) \
		-e MODEL_NAME=SUMO \
		$(FULL_IMAGE)

.PHONY: stop
stop: ## Stop the running container
	@echo "Stopping container: $(CONTAINER_NAME)"
	docker stop $(CONTAINER_NAME) || true

.PHONY: rm
rm: stop ## Remove the container
	@echo "Removing container: $(CONTAINER_NAME)"
	docker rm $(CONTAINER_NAME) || true

.PHONY: logs
logs: ## Show container logs
	docker logs -f $(CONTAINER_NAME)

.PHONY: shell
shell: ## Open a shell in the running container
	docker exec -it $(CONTAINER_NAME) /bin/bash

.PHONY: clean
clean: rm ## Clean up containers and images
	@echo "Removing image: $(FULL_IMAGE)"
	docker rmi $(FULL_IMAGE) || true

.PHONY: restart
restart: stop run ## Restart the container

.PHONY: test-health
test-health: ## Test the health endpoint
	@echo "Testing health endpoint..."
	curl -X GET http://localhost:$(PORT)/health

.PHONY: test-infer
test-infer: ## Test synchronous inference endpoint (example request)
	@echo "Testing synchronous inference endpoint..."
	curl -X POST http://localhost:$(PORT)/v2/models/SUMO/infer \
		-H "Content-Type: application/json" \
		-d @- <<'EOF'
	{
	  "id": "test_run",
	  "parameters": {
	    "name": "test1",
	    "network_url": "minio://sumo-data/test1/net.net.xml",
	    "detectors_url": "minio://sumo-data/test1/detectors.json",
	    "flow_interval_minutes": 15,
	    "simulation_datetime": "2024-01-15T08:00:00"
	  },
	  "inputs": [{
	    "name": "flow_data",
	    "shape": [6, 3],
	    "datatype": "INT32",
	    "data": [1, 0, 143, 1, 15, 120, 1, 30, 87, 2, 0, 143, 2, 15, 120, 2, 30, 87]
	  }]
	}
	EOF

.PHONY: full-deploy
full-deploy: init build push k8s-deploy k8s-status ## Complete deployment workflow: init, build, push, and deploy to k8s
	@echo "Full deployment complete!"

.PHONY: dev-setup
dev-setup: init build ## Setup for local development
	@echo "Development environment ready!"

.PHONY: info
info: ## Show current configuration
	@echo "Configuration:"
	@echo "  Image Name:     $(IMAGE_NAME)"
	@echo "  Image Tag:      $(IMAGE_TAG)"
	@echo "  Registry:       $(REGISTRY)"
	@echo "  Full Image:     $(FULL_IMAGE)"
	@echo "  Container Name: $(CONTAINER_NAME)"
	@echo "  Port:           $(PORT)"
	@echo ""
	@echo "Environment:"
	@echo "  Kafka:          $(KAFKA_BOOTSTRAP_SERVERS)"
	@echo "  MinIO:          $(MINIO_ENDPOINT)"
	@echo "  Redis:          $(REDIS_HOST):$(REDIS_PORT)"
