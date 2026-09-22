//go:build !darwin

package main

import "unsafe"

// Other platforms keep their native chrome; the page leaves the brand row
// where it is. If a host ever needs the same full-size treatment, the call
// belongs behind this file, next to its own window handle.
func styleTitlebar(unsafe.Pointer) {}

func reapplyLights(unsafe.Pointer) {}
