package main

import (
	"crypto/rand"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"net"
	"net/http"
)

//go:embed ui
var uiFS embed.FS

// serveUI publishes the embedded page on loopback and returns its URL.
//
// A file:// page would be simpler, but every engine treats one as an opaque
// origin with its own rules about modules, fetch, and storage - three sets of
// rules for one page. A loopback server is a normal http origin on all three.
//
// The path carries a random token because loopback is not private: any process
// on the machine can reach the port. The token is not much of a secret, but it
// means a stray localhost scan finds a 404 rather than the UI.
func serveUI() (string, error) {
	sub, err := fs.Sub(uiFS, "ui")
	if err != nil {
		return "", err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}

	var seed [16]byte
	if _, err := rand.Read(seed[:]); err != nil {
		return "", err
	}
	token := hex.EncodeToString(seed[:])

	mux := http.NewServeMux()
	mux.Handle("/"+token+"/", http.StripPrefix("/"+token+"/", http.FileServer(http.FS(sub))))
	go http.Serve(ln, mux)

	return fmt.Sprintf("http://%s/%s/", ln.Addr(), token), nil
}
