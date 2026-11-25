package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"sync"
	"testing"
	"time"
)

type pipeListener struct {
	connections chan net.Conn
	closed      chan struct{}
	closeOnce   sync.Once
}

func newPipeListener() *pipeListener {
	return &pipeListener{connections: make(chan net.Conn), closed: make(chan struct{})}
}

func (l *pipeListener) Accept() (net.Conn, error) {
	select {
	case connection := <-l.connections:
		return connection, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *pipeListener) Close() error {
	l.closeOnce.Do(func() { close(l.closed) })
	return nil
}

func (l *pipeListener) Addr() net.Addr { return pipeAddr("in-memory") }

func (l *pipeListener) dial() (net.Conn, error) {
	serverConn, clientConn := net.Pipe()
	select {
	case l.connections <- serverConn:
		return clientConn, nil
	case <-l.closed:
		_ = serverConn.Close()
		_ = clientConn.Close()
		return nil, net.ErrClosed
	}
}

type pipeAddr string

func (a pipeAddr) Network() string { return "pipe" }
func (a pipeAddr) String() string  { return string(a) }

func startPipeServer(t *testing.T, handler http.Handler) (*http.Server, *pipeListener) {
	t.Helper()
	listener := newPipeListener()
	server := &http.Server{Handler: handler}
	serveDone := make(chan struct{})
	go func() {
		defer close(serveDone)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		_ = server.Close()
		select {
		case <-serveDone:
		case <-time.After(time.Second):
			t.Error("test HTTP server did not exit")
		}
	})
	return server, listener
}

func sendPipeRequest(listener *pipeListener) (*http.Response, error) {
	connection, err := listener.dial()
	if err != nil {
		return nil, err
	}
	defer connection.Close()
	if _, err := fmt.Fprint(connection, "GET / HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n"); err != nil {
		return nil, err
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: http.MethodGet})
	if err == nil {
		defer response.Body.Close()
	}
	return response, err
}

func TestShutdownServerDrainsActiveRequest(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	server, listener := startPipeServer(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(started)
		<-release
		w.WriteHeader(http.StatusOK)
	}))
	responseDone := make(chan error, 1)
	go func() {
		response, err := sendPipeRequest(listener)
		if err == nil && response.StatusCode != http.StatusOK {
			err = fmt.Errorf("unexpected HTTP status %d", response.StatusCode)
		}
		responseDone <- err
	}()
	<-started
	shutdownDone := make(chan error, 1)
	go func() { shutdownDone <- shutdownServer(server, time.Second) }()
	select {
	case <-listener.closed:
	case <-time.After(time.Second):
		t.Fatal("shutdown did not close the listener before draining")
	}
	close(release)
	select {
	case err := <-shutdownDone:
		if err != nil {
			t.Fatalf("graceful shutdown failed: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("shutdown did not drain the active request")
	}
	select {
	case err := <-responseDone:
		if err != nil {
			t.Fatalf("active request did not complete: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("client request was not completed")
	}
}

func TestShutdownServerClosesConnectionsAtDeadline(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	server, listener := startPipeServer(t, http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
		close(canceled)
	}))
	responseDone := make(chan struct{})
	go func() {
		defer close(responseDone)
		_, _ = sendPipeRequest(listener)
	}()
	<-started
	shutdownStarted := time.Now()
	err := shutdownServer(server, 30*time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("expected drain deadline, got %v", err)
	}
	if elapsed := time.Since(shutdownStarted); elapsed > time.Second {
		t.Fatalf("shutdown exceeded bounded deadline: %s", elapsed)
	}
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("forced close did not cancel active handler")
	}
	select {
	case <-responseDone:
	case <-time.After(time.Second):
		t.Fatal("client request goroutine did not exit")
	}
}
