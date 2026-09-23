//go:build !darwin

package main

// styleAppMenu is a no-op off macOS, where the window's shortcuts belong to the
// window manager rather than to a menu bar the app has to build.
func styleAppMenu() {}
