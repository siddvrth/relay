# Summary

-

# Runtime Surface

- [ ] Plugin runtime or hook changed
- [ ] Goal/app-server contract changed
- [ ] Compaction/hook behavior changed
- [ ] Docs only

# Validation

- [ ] `bash validate.sh`
- [ ] Clean plugin install checked
- [ ] Real local app-server smoke checked where available

# Checklist

- [ ] `PreCompact(auto)` success/failure and manual `/compact` behavior are covered
- [ ] Destination is fresh and uses `thread/start`, not `thread/fork`
- [ ] Goal objective is restored when source Goal API is available
- [ ] No raw transcripts, secrets, private paths, or unrelated project names are committed
