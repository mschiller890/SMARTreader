# SMARTreader

a tiny, sloppy gtk app i threw together to make sense of `smartctl -x` output. my hdd was
dying so i made this to get readable info out of the mess -- quick and dirty, but it helps lol

im really lazy to write this properly sorry

what it does
- detects drives (sda, nvme0n1, etc)
- runs `smartctl -x` (uses `pkexec` if you're not root)
- parses model, serial, health, metrics (temp, pow-on hours, reallocated sectors),
  SMART attributes, and error logs

requirements
- python 3.10+
- smartmontools (`smartctl`)
- pygobject / gtk4 / libadwaita bindings (your distro may call these different names)

quick install (arch-ish)
```sh
sudo pacman -S smartmontools python python-gobject gtk4 libadwaita
```

run
```sh
python3 main.py
```

notes
- if you don't run as root the app will try `pkexec` -- expect an auth prompt
- running self-tests can take a while and put load on the drive; be careful
- parser is in `main.py` as `SmartParser` -- PRs welcome if your vendor prints garbage
