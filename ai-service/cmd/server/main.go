package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"example.com/pfp/ai-service/internal/analysis"
)

func main() {
	token := os.Getenv("PFP_AI_INTERNAL_TOKEN")
	if len(token) < 24 {
		log.Fatal("PFP_AI_INTERNAL_TOKEN must contain at least 24 characters")
	}
	service := analysis.NewService(analysis.DeterministicModel{})
	srv := &http.Server{
		Addr:              ":8091",
		Handler:           analysis.Handler(service, token),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
	}
	log.Print("AI analysis development service listening on :8091 (fake LLM)")
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.ListenAndServe() }()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	select {
	case sig := <-signals:
		log.Printf("received %s; draining HTTP requests", sig)
		if err := shutdownServer(srv, 10*time.Second); err != nil {
			log.Printf("graceful shutdown deadline reached: %v", err)
		}
		if err := <-serveErr; err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("HTTP server stopped with error: %v", err)
		}
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}
}

func shutdownServer(server *http.Server, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		// Shutdown stops waiting at the deadline but does not close active
		// connections. Force-close them so requests receive cancellation.
		_ = server.Close()
		return err
	}
	return nil
}
