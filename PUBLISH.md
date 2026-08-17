# PUBLISH.md — publishing this repository

Steps for the maintainer to publish CylinderUI to a public git host. Nothing
here runs automatically; you execute each command yourself.

## 0. Before every commit — run the secret scan

`secret-scan.sh` is the publication gate. It fails (exit 1) on real leaks
(secrets, private IPs, personal paths, and private identity/branding) and
separately reports allowlisted technical identifiers without failing.

```bash
./secret-scan.sh          # scans the publish set (repo/)
echo $?                    # must be 0 before you commit
```

Wire it as a pre-commit hook so it runs on every commit:

```bash
printf '#!/usr/bin/env bash\nexec ./secret-scan.sh\n' > .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## 1. First-time setup

```bash
git init
git add .
git commit -m "Initial public release: CylinderUI for Llama.cpp"
git branch -M main
git remote add origin https://github.com/fdfreitas88/cylinderui.git
git push -u origin main
```

## 2. Subsequent changes

```bash
./secret-scan.sh && echo OK    # gate first
git add -A
git commit -m "<message>"
git push
```

The repository uses the complete GNU GPL v3 text in `LICENSE`. Preserve that
file and the project copyright notices when distributing the software.

---

## Rewriting history — IMPORTANT if you reused an older repo

The **personal** `index.html` (the private, personally-branded build) was
committed earlier in this project's history. The clean `router/index.html` here
is neutral, but if you publish on top of an existing repo that already contains
those earlier commits, the personal file is still recoverable from history. You
must **purge it from every commit** before pushing publicly.

> Only do this on a repo you control, after making a backup. History rewriting
> changes every commit hash; coordinate if anyone else has clones.

### Option A — git filter-repo (recommended)

```bash
# install: pipx install git-filter-repo   (or: pip install git-filter-repo)
git filter-repo --path index.html --invert-paths
# also strip any other personal artifacts that ever landed, e.g.:
git filter-repo --path CylinderUI.png --path console-index.html --invert-paths
```

### Option B — BFG Repo-Cleaner

```bash
# https://rtyley.github.io/bfg-repo-cleaner/
bfg --delete-files index.html.OLD          # match the personal file name(s)
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

### After purging

```bash
./secret-scan.sh && echo OK        # confirm the tree is clean
git push --force-with-lease origin main
```

Then verify the pushed history no longer contains the personal file:

```bash
git log --all --oneline -- index.html   # should print nothing for the old path
```
