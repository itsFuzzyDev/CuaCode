//go:build !darwin

package main

// pickFolder and pickImage are darwin-only so far (titlebar_darwin.go set
// the pattern). A Linux/Windows picker lands when those platforms are real
// for this frontend.
func pickFolder() string { return "" }

func pickImage() string { return "" }
