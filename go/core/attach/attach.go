// Package attach is what a picture has to go through before it can be part of
// a message: read off disk or off the system clipboard, checked for being an
// image at all, and base64'd, which is the form the wire and every provider
// both want.
//
// It lives under core/ because both frontends need it and they need it to
// behave identically - a file the terminal refuses and the window accepts is a
// bug report nobody can reproduce.
package attach

import (
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// Image is one picture waiting to be sent: its name, for the frontends to
// show and the worker to record, and its bytes already base64'd, which is the
// form both the wire and every provider want.
type Image struct {
	Name string `json:"name"`
	B64  string `json:"b64"`
	Size int    `json:"size"` // decoded bytes, for the row that says how big it is
	// What the bytes turned out to be, from the sniff rather than from the
	// name. The window needs it to draw a thumbnail: a data URL has to say
	// what it is carrying.
	Mime string `json:"mime"`
}

// MaxImage is the largest Image accepted. The limit is not the wire -
// the worker's stdin takes a line of any length - it is the round trip: an
// 8MB photo is ~11MB of base64 re-uploaded on every round of the turn, and a
// model reads a 1024px picture just as well.
const MaxImage = 8 << 20

// imageKinds maps the magic bytes at the head of a file to the extension the
// picture actually is. Sniffed rather than trusted from the name: a .png that
// is really a PDF is a 400 from the provider, several seconds later, blamed on
// nothing in particular.
var imageKinds = []struct {
	magic string
	ext   string
}{
	{"\x89PNG\r\n\x1a\n", "png"},
	{"\xff\xd8\xff", "jpg"},
	{"GIF87a", "gif"},
	{"GIF89a", "gif"},
	{"RIFF", "webp"}, // RIFF....WEBP; the container is checked below
}

// sniff reports the image kind of these bytes, or "" for something that is not
// an image any provider takes.
func Sniff(b []byte) string {
	for _, k := range imageKinds {
		if !strings.HasPrefix(string(b), k.magic) {
			continue
		}
		if k.ext == "webp" && !(len(b) >= 12 && string(b[8:12]) == "WEBP") {
			continue
		}
		return k.ext
	}
	return ""
}

// looksLikeImage is the cheap half of the test: whether a path is worth
// opening at all. absorbPath runs on keystrokes, so the extension is checked
// before anything touches the disk.
func LooksLikeImage(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".png", ".jpg", ".jpeg", ".gif", ".webp":
		return true
	}
	return false
}

// readImage loads a file as an Image. It is the only place a file becomes
// one, so the size cap and the sniff apply however the path arrived.
func ReadFile(path string) (Image, error) {
	info, err := os.Stat(path)
	if err != nil {
		return Image{}, err
	}
	if info.IsDir() {
		return Image{}, fmt.Errorf("%s is a directory", filepath.Base(path))
	}
	if info.Size() > MaxImage {
		return Image{}, fmt.Errorf("%s is %s - the limit is %s",
			filepath.Base(path), FmtBytes(int(info.Size())), FmtBytes(MaxImage))
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return Image{}, err
	}
	return FromBytes(filepath.Base(path), b)
}

func FromBytes(name string, b []byte) (Image, error) {
	kind := Sniff(b)
	if kind == "" {
		return Image{}, fmt.Errorf("%s is not a png, jpeg, gif or webp", name)
	}
	if len(b) > MaxImage {
		return Image{}, fmt.Errorf("%s is %s - the limit is %s",
			name, FmtBytes(len(b)), FmtBytes(MaxImage))
	}
	return Image{Name: name, B64: base64.StdEncoding.EncodeToString(b),
		Size: len(b), Mime: mimeOf(kind)}, nil
}

// mimeOf is the media type for a sniffed kind. Spelled out rather than
// "image/"+kind: the extension and the media type agree for three of the four
// and not for the one everybody meets first.
func mimeOf(kind string) string {
	if kind == "jpg" {
		return "image/jpeg"
	}
	return "image/" + kind
}

func FmtBytes(n int) string {
	switch {
	case n >= 1<<20:
		return fmt.Sprintf("%.1fMB", float64(n)/(1<<20))
	case n >= 1<<10:
		return fmt.Sprintf("%dKB", n/(1<<10))
	}
	return fmt.Sprintf("%dB", n)
}

// ------------------------------------------------------------ the clipboard

// readClipboard returns the picture the system clipboard is holding.
//
// Not through the terminal: OSC 52 carries text and most terminals will not
// answer a read of it at all, and Cmd+V is the terminal pasting *its* idea of
// the clipboard, which for an image is nothing. The clipboard is the OS's, so
// the OS is asked directly.
//
// A file copied in a file manager counts as a picture: it is the same gesture,
// and the name it comes back with is better than the one an anonymous bitmap
// would get.
func Clipboard() (Image, error) {
	switch runtime.GOOS {
	case "darwin":
		return clipDarwin()
	case "windows":
		return clipWindows()
	default:
		return clipUnix()
	}
}

// errNoImage is what every platform says when the clipboard holds something
// that is not a picture, which is most of the time and is not a failure.
var errNoImage = fmt.Errorf("no image on the clipboard")

// clipScript is AppleScript because the pasteboard is: `pbpaste` deals in text
// and has no flag that will hand over a PNG. The three classes are tried in
// the order that keeps the most information - a copied file keeps its name, a
// PNG needs no conversion, and TIFF is what Preview and the older apps put
// there and has to be turned into something a provider accepts.
const clipScript = `on run argv
	set outPath to item 1 of argv
	try
		return "file:" & (POSIX path of (the clipboard as «class furl»))
	end try
	try
		set d to the clipboard as «class PNGf»
		my writeTo(d, outPath)
		return "png"
	end try
	try
		set d to the clipboard as «class TIFF»
		my writeTo(d, outPath)
		return "tiff"
	end try
	return "none"
end run

on writeTo(d, outPath)
	set fh to open for access (POSIX file outPath) with write permission
	set eof fh to 0
	write d to fh
	close access fh
end writeTo`

func clipDarwin() (Image, error) {
	tmp, err := os.CreateTemp("", "cuacode-clip-*")
	if err != nil {
		return Image{}, err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())

	cmd := exec.Command("osascript", "-", tmp.Name())
	cmd.Stdin = strings.NewReader(clipScript)
	out, err := cmd.Output()
	if err != nil {
		return Image{}, errNoImage
	}

	switch verdict := strings.TrimSpace(string(out)); {
	case strings.HasPrefix(verdict, "file:"):
		path := strings.TrimPrefix(verdict, "file:")
		if !LooksLikeImage(path) {
			return Image{}, errNoImage
		}
		return ReadFile(path)

	case verdict == "png":
		// Renamed off the temp file: nothing copied from a screen has a name,
		// and "cuacode-clip-968985836" is a worse answer than admitting that.
		att, err := ReadFile(tmp.Name())
		att.Name = "clipboard.png"
		return att, err

	case verdict == "tiff":
		// sips ships with macOS and is the only converter guaranteed to be
		// there. Without it the bytes are a TIFF, which no provider takes.
		png := tmp.Name() + ".png"
		defer os.Remove(png)
		if err := exec.Command("sips", "-s", "format", "png", tmp.Name(), "--out", png).Run(); err != nil {
			return Image{}, errNoImage
		}
		att, err := ReadFile(png)
		att.Name = "clipboard.png"
		return att, err
	}
	return Image{}, errNoImage
}

// clipUnix asks whichever clipboard this session actually has. Wayland first
// because a wayland session can still have an X clipboard behind Xwayland, and
// the one the user copied into is the compositor's.
func clipUnix() (Image, error) {
	tries := [][]string{
		{"wl-paste", "--no-newline", "--type", "image/png"},
		{"xclip", "-selection", "clipboard", "-t", "image/png", "-o"},
		{"xsel", "--clipboard", "--output"},
	}
	for _, argv := range tries {
		if _, err := exec.LookPath(argv[0]); err != nil {
			continue
		}
		out, err := exec.Command(argv[0], argv[1:]...).Output()
		if err != nil || len(out) == 0 {
			continue
		}
		if Sniff(out) == "" {
			continue
		}
		return FromBytes("clipboard.png", out)
	}
	return Image{}, errNoImage
}

// clipPS runs in STA because the Windows clipboard API refuses to be read from
// any other apartment, and fails by returning nothing rather than by saying so.
const clipPS = `Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$files = [Windows.Forms.Clipboard]::GetFileDropList()
if ($files.Count -gt 0) { "file:" + $files[0]; exit }
$img = [Windows.Forms.Clipboard]::GetImage()
if ($img) { $img.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png); "png"; exit }
"none"`

func clipWindows() (Image, error) {
	tmp, err := os.CreateTemp("", "cuacode-clip-*.png")
	if err != nil {
		return Image{}, err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())

	out, err := exec.Command("powershell", "-NoProfile", "-STA", "-Command", clipPS, tmp.Name()).Output()
	if err != nil {
		return Image{}, errNoImage
	}
	switch verdict := strings.TrimSpace(string(out)); {
	case strings.HasPrefix(verdict, "file:"):
		path := strings.TrimPrefix(verdict, "file:")
		if !LooksLikeImage(path) {
			return Image{}, errNoImage
		}
		return ReadFile(path)
	case verdict == "png":
		att, err := ReadFile(tmp.Name())
		att.Name = "clipboard.png"
		return att, err
	}
	return Image{}, errNoImage
}
