#!/usr/bin/env bash
set -euo pipefail
# Owner-run build in a new, empty directory. No account creation or camera access.
commit=1016a7626341729d2c47188e63b81fa14a3d48af
if [[ $# != 1 || -e $1 ]]; then
  echo 'Usage: tools/build-mdk.sh NEW_BUILD_DIRECTORY' >&2
  exit 1
fi
mkdir -m 700 "$1"
root=$(cd "$1" && pwd)
git clone https://github.com/marmot-protocol/mdk.git "$root/source"
git -C "$root/source" checkout --detach "$commit"
test "$(git -C "$root/source" rev-parse HEAD)" = "$commit"
(cd "$root/source" && cargo build --locked --release -p wn-cli --bin wn --bin wnd)
mkdir -m 700 "$root/bin"
install -m 700 "$root/source/target/release/wn" "$root/source/target/release/wnd" "$root/bin/"
python3 - "$root/bin" "$commit" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1])
(p/'mdk-provenance.json').write_text(json.dumps({
 'commit':sys.argv[2], 'sha256':{n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in ('wn','wnd')}
},indent=2)+'\n')
(p/'mdk-provenance.json').chmod(0o600)
PY
printf 'MDK binaries built in %s/bin. No identity or service has been started.\n' "$root"
