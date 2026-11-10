//! `cuacode doctor`: which build this is, where it came from, and whether origin
//! has moved past it.
//!
//! This lives in the shared crate rather than the GUI so Windows can answer the
//! question without building or opening the GUI, and so every frontend and the
//! launcher use one implementation. It never starts the worker or a session.
//! `doctor --upgrade` is the same answer acted on: pull, rebuild, restart, and
//! print every step because a pull and a build that fail are things to read.

use std::path::Path;
#[cfg(windows)]
use std::path::PathBuf;

use crate::version::{self, News};
use crate::worker;

#[derive(Debug, Default, PartialEq, Eq)]
struct Options {
    upgrade: bool,
    help: bool,
}

/// Print the build, checkout, update, worker, and state-root report.
///
/// A leading `doctor` is accepted so a launcher can forward its whole command
/// line without having to remove the subcommand first. `--upgrade` fetches the
/// branch the remote names as its default, fast-forwards onto it, and then
/// rebuilds the platform's frontends.
pub fn run(args: &[String]) -> i32 {
    let options = match parse_args(args) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("cuacode doctor: {message}");
            eprintln!("run `cuacode doctor --help` for usage");
            return 2;
        }
    };
    if options.help {
        print_help();
        return 0;
    }

    let root = version::root();
    println!("{:<8} {}", "cuacode", version::ID);
    println!(
        "{:<8} {}",
        "commit",
        if version::COMMIT.is_empty() {
            "unknown, this build had no git"
        } else {
            version::COMMIT
        }
    );
    match &root {
        Some(dir) => {
            let branch =
                version::git(dir, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap_or_default();
            println!("{:<8} {}  branch {branch}", "tree", dir.display());
            println!(
                "{:<8} {}",
                "origin",
                version::git(dir, &["remote", "get-url", "origin"])
                    .unwrap_or_else(|_| "no remote".into())
            );
        }
        None => println!(
            "{:<8} not inside a checkout, so updates cannot be asked for",
            "tree"
        ),
    }
    match version::check() {
        News::Behind(n) => println!(
            "{:<8} behind {n} {} - run cuacode doctor --upgrade",
            "updates",
            if n == 1 { "update" } else { "updates" }
        ),
        News::Current => println!("{:<8} up to date with origin", "updates"),
        News::Unknown(why) => println!("{:<8} cannot say: {why}", "updates"),
    }
    match worker::worker_path() {
        Ok(path) => println!(
            "{:<8} {} via {}",
            "worker",
            path.display(),
            worker::python(&path)
                .map(|p| p.display().to_string())
                .unwrap_or_else(|e| e)
        ),
        Err(why) => println!("{:<8} {why}", "worker"),
    }
    println!("{:<8} {}", "state", crate::state_root().display());

    if !options.upgrade {
        return 0;
    }
    let Some(root) = root else {
        eprintln!("cuacode: --upgrade needs a checkout; this binary is not inside one");
        return 1;
    };
    println!();
    if let Err(code) = update(&root) {
        return code;
    }
    // After the update, because the merge is what brings a new pin in, and
    // before the rebuild, because nothing builds without the tree it names.
    if let Err(code) = ensure_vendored(&root) {
        return code;
    }
    if let Err(code) = rebuild(&root) {
        return code;
    }
    println!("\ndone - restart the app to run the new build");
    0
}

fn parse_args(args: &[String]) -> Result<Options, String> {
    let mut options = Options::default();
    let mut args = args;
    if args
        .first()
        .is_some_and(|arg| arg.eq_ignore_ascii_case("doctor"))
    {
        args = &args[1..];
    }
    for arg in args {
        match arg.as_str() {
            "--upgrade" => options.upgrade = true,
            "--help" | "-h" => options.help = true,
            other => return Err(format!("unknown argument `{other}`")),
        }
    }
    Ok(options)
}

fn print_help() {
    println!(
        "CuaCode doctor\n\
         \n\
         Usage: cuacode doctor [--upgrade]\n\
         \n\
         Print the build, checkout, update, worker, and state-root report.\n\
         --upgrade fast-forwards the remote's default branch and rebuilds the\n\
         frontends.\n\
         -h, --help shows this help."
    );
}

/// Rebuild the frontends this platform can run. Unix keeps the tree's own
/// `build.sh`; Windows calls Cargo directly so neither WSL nor Git Bash is a
/// dependency.
fn rebuild(root: &Path) -> Result<(), i32> {
    #[cfg(windows)]
    {
        rebuild_windows(root)
    }
    #[cfg(not(windows))]
    {
        let build = root.join("build.sh");
        if !build.is_file() {
            eprintln!(
                "cuacode: {} is missing, so nothing was rebuilt",
                build.display()
            );
            return Err(1);
        }
        step(root, "./build.sh", &[])
    }
}

#[cfg(windows)]
fn rebuild_windows(root: &Path) -> Result<(), i32> {
    let rust = root.join("rust");
    if !rust.is_dir() {
        eprintln!(
            "cuacode: {} is missing, so nothing was rebuilt",
            rust.display()
        );
        return Err(1);
    }
    let Some(cargo) = find_cargo() else {
        eprintln!("cuacode: cargo was not found on PATH or in ~/.cargo/bin");
        return Err(1);
    };
    let dev_cmd = find_vs_dev_cmd();

    // Before building, take away the images a previous upgrade had to move
    // aside. The ones still owned by a live process cannot be deleted and are
    // left alone, so the frontend running this is never damaged.
    sweep_asides(&rust.join("target").join("release"));

    // Doctor first, in a target directory of its own: cargo may not write the
    // image of the process running this function on Windows, and the launcher
    // swaps the staged copy in on the next launch. Doing it before the
    // frontends matters - a frontend build that fails must not be the reason a
    // tree stays stuck on an old doctor, since doctor is what reports the
    // failure.
    if rust.join("src/bin/cuacode-doctor.rs").is_file() {
        let target = rust.join("target").join("doctor-next");
        cargo_build(
            &cargo,
            dev_cmd.as_deref(),
            &rust,
            &["--release", "-p", "cuacode-rs", "--bin", "cuacode-doctor"],
            Some(&target),
        )?;
        let built = target.join("release").join("cuacode-doctor.exe");
        let release = rust.join("target").join("release");
        let next = release.join("cuacode-doctor.next.exe");
        if let Err(e) = std::fs::create_dir_all(&release) {
            eprintln!("cuacode: could not create {}: {e}", release.display());
            return Err(1);
        }
        if let Err(e) = std::fs::copy(&built, &next) {
            eprintln!(
                "cuacode: could not stage {} as {}: {e}",
                built.display(),
                next.display()
            );
            return Err(1);
        }
        println!("staged the next doctor at {}", next.display());
    }

    // One package and one binary per call, which is the pairing every cargo
    // version agrees on: a `-p` list beside a `--bin` list can be read as "the
    // bins of the default package" and fail with "no bin target named `quik`".
    build_frontend(&cargo, dev_cmd.as_deref(), &rust, "cuacode-quik", "quik")?;
    build_frontend(&cargo, dev_cmd.as_deref(), &rust, "cuacode-fastrs", "fastrs")?;
    Ok(())
}

/// Build one frontend, moving a running image of it out of the way first.
///
/// `doctor --upgrade` is usually run *from* the frontend being upgraded - the
/// TUI you are typing into is `quik.exe`, and the file cargo wants to write is
/// the image of the process asking. Windows refuses to open a running image for
/// write (`os error 5`), which used to end the whole upgrade on the first
/// frontend and leave the rest stale.
///
/// It does allow the file to be *renamed*: the process keeps running from the
/// moved image, unaffected, and a fresh binary can be written at the old name.
/// `cuacode-doctor` already relies on the same two facts to stage its successor;
/// this is that trick applied to the frontends.
#[cfg(windows)]
fn build_frontend(
    cargo: &Path,
    dev_cmd: Option<&Path>,
    rust: &Path,
    package: &str,
    bin: &str,
) -> Result<(), i32> {
    let exe = rust
        .join("target")
        .join("release")
        .join(format!("{bin}.exe"));
    if let Some(aside) = move_running_aside(&exe) {
        println!(
            "{bin}.exe is running - moved it to {} so the new one can be written; \
             this session keeps running, the next launch is the new build",
            aside.display()
        );
    }
    cargo_build(
        cargo,
        dev_cmd,
        rust,
        &["--release", "-p", package, "--bin", bin],
        None,
    )
}

/// Move `exe` to a dated sibling if, and only if, it cannot be opened for write.
///
/// A file that opens for write is not running and cargo will replace it itself,
/// so nothing is renamed in the ordinary case. The stamp keeps repeated upgrades
/// from colliding on one name; `sweep_asides` takes them away again once the
/// process that held them has gone.
#[cfg(windows)]
fn move_running_aside(exe: &Path) -> Option<PathBuf> {
    if !exe.is_file() || std::fs::OpenOptions::new().write(true).open(exe).is_ok() {
        return None;
    }
    let aside = exe.with_extension(format!("exe.old-{}", stamp()));
    std::fs::rename(exe, &aside).ok().map(|()| aside)
}

/// Delete the moved-aside images a previous upgrade left beside the frontends.
///
/// These are copies of a binary that was running when it was replaced. Windows
/// refuses to delete one for as long as its process holds the image - which is
/// the property that makes this safe to run unconditionally: the frontend you
/// are typing into keeps its image, everything already exited goes.
///
/// `.running-` is the name an earlier build used for the same thing, so a tree
/// that has been upgraded across that change collects both.
#[cfg(windows)]
fn sweep_asides(dir: &Path) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.contains(".exe.old-") && !name.contains(".exe.running-") {
            continue;
        }
        // Best effort by design. A failure here means a live process still owns
        // the image, and saying so every launch would be noise about a file that
        // is going to disappear on its own.
        let _ = std::fs::remove_file(&path);
    }
}

/// `YYYYMMDD-HHMMSS`, for a name beside the executable.
#[cfg(windows)]
fn stamp() -> String {
    // Local time would mean a timezone database; the name only has to be unique
    // and sortable, and UTC is both.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (y, m, d) = civil_from_days((secs / 86_400) as i64);
    let rem = secs % 86_400;
    format!(
        "{y:04}{m:02}{d:02}-{:02}{:02}{:02}",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// Days since 1970-01-01 to a calendar date. Howard Hinnant's algorithm, which
/// is exact for every date this program will see and needs no table.
#[cfg(windows)]
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(windows)]
fn cargo_build(
    cargo: &Path,
    dev_cmd: Option<&Path>,
    rust: &Path,
    args: &[&str],
    target_dir: Option<&Path>,
) -> Result<(), i32> {
    println!("$ cargo build {}", args.join(" "));

    // rustc finds link.exe, but it does not find the MSVC and Windows SDK
    // libraries unless something names them: VsDevCmd is what supplies LIB and
    // INCLUDE. `-arch` has to be said out loud. VsDevCmd's own default is x86
    // whatever the machine is, and that points LIB at the 32-bit libraries,
    // which links nothing for a 64-bit target.
    let mut command = if let Some(dev_cmd) = dev_cmd {
        let mut command = std::process::Command::new("cmd.exe");
        // Pass the batch file as its own token. Putting the whole command in
        // one string makes Rust escape the embedded quotes for CreateProcess,
        // but cmd.exe does not understand backslash-escaped quotes.
        command.args(["/d", "/c", "call"]).arg(dev_cmd);
        if let Some(arch) = vs_arch() {
            command.arg(format!("-arch={arch}"));
        }
        command
            .args([">nul", "&&"])
            .arg(cargo)
            .arg("build")
            .args(args);
        command
    } else {
        // No developer environment: cargo links with whatever libraries this
        // process inherited, which on a plain shell is none, and the failure
        // arrives two screens later as a linker complaint about msvcrt.lib.
        // Say it here, where it is the answer.
        eprintln!(
            "cuacode: no Visual Studio developer environment found, so cargo runs with this shell's environment"
        );
        let mut command = std::process::Command::new(cargo);
        command.arg("build").args(args);
        command
    };
    if let Some(target_dir) = target_dir {
        command.env("CARGO_TARGET_DIR", target_dir);
    }
    report_status("cargo", command.current_dir(rust).status())
}

#[cfg(windows)]
fn find_cargo() -> Option<PathBuf> {
    for variable in ["CUACODE_CARGO", "CARGO"] {
        if let Some(path) = std::env::var_os(variable).map(PathBuf::from) {
            if path.is_file() {
                return Some(path);
            }
        }
    }
    if let Some(path) = on_path("cargo.exe") {
        return Some(path);
    }
    let path = crate::home_dir()?.join(".cargo").join("bin").join("cargo.exe");
    path.is_file().then_some(path)
}

#[cfg(windows)]
fn on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

/// The arch VsDevCmd should set up: the one this binary was built for, which
/// is what cargo builds for unless a target was named.
#[cfg(windows)]
fn vs_arch() -> Option<&'static str> {
    match std::env::consts::ARCH {
        "x86_64" => Some("x64"),
        "aarch64" => Some("arm64"),
        _ => None,
    }
}

#[cfg(windows)]
fn find_vs_dev_cmd() -> Option<PathBuf> {
    let program_files_x86 = std::env::var_os("ProgramFiles(x86)")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Program Files (x86)"));
    let vswhere = program_files_x86
        .join("Microsoft Visual Studio")
        .join("Installer")
        .join("vswhere.exe");
    if !vswhere.is_file() {
        return None;
    }
    // The component id is Visual Studio 2022's. It is asked for first because it
    // is the exact match, and a plain search for the batch file is the fallback,
    // because a version that names its C++ workload differently answers nothing
    // to the first query and the caller is then told there is no toolchain.
    let queries: [&[&str]; 2] = [
        &[
            "-all",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        &["-all", "-products", "*", "-find", "**\\Common7\\Tools\\VsDevCmd.bat"],
    ];
    for args in queries {
        let Some(output) = std::process::Command::new(&vswhere)
            .args(args)
            .output()
            .ok()
            .filter(|output| output.status.success())
        else {
            continue;
        };
        for line in String::from_utf8_lossy(&output.stdout).lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            // One query prints an install directory, the other a full path.
            let named = PathBuf::from(line);
            let candidate = if named.is_file() {
                named
            } else {
                named.join("Common7").join("Tools").join("VsDevCmd.bat")
            };
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Make sure the vendored trees the workspace links by path are on disk.
///
/// A path dependency that is not there is not a missing crate: cargo resolves the
/// whole workspace before it builds anything, so `zeron-ui` absent stops even
/// `-p cuacode-rs --bin cuacode-doctor`. That is the command whose job is to say
/// what is wrong, failing with a message that does not mention this function's
/// subject at all. `.gitignore` keeps `rust/zeron` out of the repository, so a
/// fresh clone has to fetch it, and until now only a person who read the GUI's
/// own source knew to.
///
/// The pin lives in `rust/zeron.toml` so a script and this function read the same
/// answer. A tree that is already there at the right rev costs no network.
fn ensure_vendored(root: &Path) -> Result<(), i32> {
    let rust = root.join("rust");
    let pin = rust.join("zeron.toml");
    if !pin.is_file() {
        // No pin, no path dependency to satisfy: an older checkout.
        return Ok(());
    }
    let Some(dep) = read_pin(&pin) else {
        eprintln!(
            "cuacode: {} does not name both `repo` and `rev`",
            pin.display()
        );
        return Err(1);
    };
    let tree = rust.join("zeron");

    // A path dependency needs `crates/ui`, not merely a directory.
    if tree.join("crates/ui/Cargo.toml").is_file() {
        let at = version::git(&tree, &["describe", "--tags", "--always"]).unwrap_or_default();
        if at == dep.rev {
            return Ok(());
        }
        // Rename, not `git checkout`: a half-written tree from an interrupted
        // clone is not something to guess at, and the vendored tree is never
        // where anyone's work lives.
        let aside = rust.join("zeron.stale");
        let _ = std::fs::remove_dir_all(&aside);
        if let Err(e) = std::fs::rename(&tree, &aside) {
            eprintln!(
                "cuacode: could not move {} to {}: {e}",
                tree.display(),
                aside.display()
            );
            return Err(1);
        }
        println!("the vendored zeron tree was {at}, wanted {}", dep.rev);
    }

    println!(
        "fetching the vendored zeron tree ({}) - once, it is not in the repository",
        dep.rev
    );
    // Shallow: the pin is a tag, the history is not linked against, and a full
    // clone of this repository is a few hundred megabytes for nothing.
    let cloned = step(
        &rust,
        "git",
        &[
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--branch",
            &dep.rev,
            "--",
            &dep.repo,
            "zeron",
        ],
    );
    match cloned {
        Ok(()) => {
            println!("vendored zeron {} at {}", dep.rev, tree.display());
            Ok(())
        }
        Err(code) => Err(code),
    }
}

/// `repo` and `rev` out of `rust/zeron.toml`.
fn read_pin(path: &Path) -> Option<Pin> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut repo = None;
    let mut rev = None;
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"');
        match key.trim() {
            "repo" => repo = Some(value.to_string()),
            "rev" => rev = Some(value.to_string()),
            _ => {}
        }
    }
    Some(Pin {
        repo: repo?,
        rev: rev?,
    })
}

struct Pin {
    repo: String,
    rev: String,
}

/// Fast-forward this checkout from the remote's default branch.
///
/// Not `git pull`: that follows the local branch's configured upstream, and a
/// checkout cloned from a fork keeps pointing at the fork after its work has
/// landed upstream. The remote itself names the branch to follow, and an
/// explicit fetch and `--ff-only` merge asks that branch, leaves the working
/// tree alone when it cannot be fast-forwarded, and never rebases or merges.
fn update(root: &Path) -> Result<(), i32> {
    let (remote, branch) = match version::upstream(root) {
        Ok(target) => target,
        Err(why) => {
            eprintln!("cuacode: cannot find the branch to update from: {why}");
            return Err(1);
        }
    };
    println!("updating from {remote}/{branch}");
    if let Err(code) = step(root, "git", &["fetch", "--quiet", &remote]) {
        return Err(code);
    }
    let target = format!("{remote}/{branch}");
    step(root, "git", &["merge", "--ff-only", &target])
}

/// Run one step of the upgrade in the tree, with its output going straight to
/// the terminal: a build that is quiet until it fails is a build nobody trusts.
fn step(dir: &Path, program: &str, args: &[&str]) -> Result<(), i32> {
    println!("$ {program} {}", args.join(" "));
    let result = std::process::Command::new(program)
        .args(args)
        .current_dir(dir)
        .status();
    report_status(program, result)
}

fn report_status(
    program: &str,
    result: std::io::Result<std::process::ExitStatus>,
) -> Result<(), i32> {
    match result {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => {
            eprintln!("cuacode: {program} exited {status}, stopping here");
            Err(1)
        }
        Err(e) => {
            eprintln!("cuacode: could not run {program}: {e}");
            Err(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn a_launcher_can_forward_the_doctor_subcommand() {
        assert_eq!(
            parse_args(&args(&["doctor", "--upgrade"])).unwrap(),
            Options {
                upgrade: true,
                help: false
            }
        );
        assert_eq!(
            parse_args(&args(&["--help"])).unwrap(),
            Options {
                upgrade: false,
                help: true
            }
        );
        assert_eq!(
            parse_args(&args(&["DOCTOR", "--upgrade"])).unwrap(),
            Options {
                upgrade: true,
                help: false
            }
        );
    }

    #[test]
    fn a_prompt_that_is_not_doctor_is_not_silently_ignored() {
        assert!(parse_args(&args(&["explain this"])).is_err());
    }

    /// The pin is hand-edited, so the reader has to accept the shapes a person
    /// writes: quoted, bare, indented, with a comment after it.
    #[test]
    fn the_pin_reads_the_shapes_a_person_writes() {
        let dir = std::env::temp_dir().join("cuacode-pin-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("zeron.toml");
        let cases = [
            ("repo = \"https://x/y\"\nrev = \"v1.2.3\"\n", "https://x/y", "v1.2.3"),
            ("repo = https://x/y\nrev = v1.2.3\n", "https://x/y", "v1.2.3"),
            ("  repo  =  \"https://x/y\"\n  rev = \"v1\"\n", "https://x/y", "v1"),
            (
                "# a note\nrepo = \"https://x/y\"  # and why\nrev = \"v1\"\n",
                "https://x/y",
                "v1",
            ),
        ];
        for (text, repo, rev) in cases {
            std::fs::write(&path, text).unwrap();
            let pin = read_pin(&path).unwrap_or_else(|| panic!("no pin out of {text:?}"));
            assert_eq!(pin.repo, repo, "repo from {text:?}");
            assert_eq!(pin.rev, rev, "rev from {text:?}");
        }

        // Half a pin is not a pin: cloning with an empty rev would fetch the
        // default branch and silently ignore what the file says.
        std::fs::write(&path, "repo = \"https://x/y\"\n").unwrap();
        assert!(read_pin(&path).is_none());
        std::fs::write(&path, "rev = \"v1\"\n").unwrap();
        assert!(read_pin(&path).is_none());

        std::fs::remove_dir_all(&dir).ok();
    }

    /// The stamp names the file an upgrade moved a running image to. Two in the
    /// same second must not collide into one name, and it has to sort.
    #[test]
    fn the_stamp_is_a_sortable_utc_timestamp() {
        let stamp = stamp();
        assert_eq!(stamp.len(), 15, "got {stamp:?}");
        assert_eq!(&stamp[8..9], "-", "got {stamp:?}");
        assert!(
            stamp.chars().enumerate().all(|(i, c)| i == 8 || c.is_ascii_digit()),
            "got {stamp:?}"
        );
        let (y, m, d) = civil_from_days(0);
        assert_eq!((y, m, d), (1970, 1, 1));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
        assert_eq!(civil_from_days(20_725), (2026, 9, 29));
    }

    /// An aside is only swept once the process that held it has gone. That is
    /// Windows' rule, not ours, so what is testable here is the filter: a file
    /// that is not an `.exe.old-` copy is left alone, whatever this does to it.
    #[test]
    fn the_sweep_only_touches_aside_images() {
        let dir = std::env::temp_dir().join(format!("cuacode-sweep-test-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(&dir).unwrap();
        let keep = dir.join("quik.exe");
        let drop = dir.join("quik.exe.old-20261108-120000");
        let legacy = dir.join("quik.exe.running-20261028-170425");
        let other = dir.join("notes.txt");
        std::fs::write(&keep, b"x").unwrap();
        std::fs::write(&drop, b"x").unwrap();
        std::fs::write(&legacy, b"x").unwrap();
        std::fs::write(&other, b"x").unwrap();

        sweep_asides(&dir);

        assert!(keep.is_file(), "a live frontend's image must survive");
        assert!(!drop.exists(), "a moved-aside image should be gone");
        assert!(
            !legacy.exists(),
            "the name an earlier build used is the same thing"
        );
        assert!(other.is_file(), "unrelated files are not ours");
        std::fs::remove_dir_all(&dir).ok();
    }
}
