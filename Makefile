.PHONY: build run clean test

build:
	go build -o ./devops-bot ./cmd/devops-bot

run: build
	./devops-bot --config ./configs/config.yaml --rules ./configs/rules.yaml

clean:
	rm -f ./devops-bot
	rm -rf ./data

test:
	go test ./...

lint:
	golangci-lint run
