FROM golang:1.23-alpine AS build

WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/pfp-ai ./cmd/server

FROM alpine:3.21

RUN apk add --no-cache ca-certificates
COPY --from=build /out/pfp-ai /usr/local/bin/pfp-ai

EXPOSE 8091
ENTRYPOINT ["pfp-ai"]
