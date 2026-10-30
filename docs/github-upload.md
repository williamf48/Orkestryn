# Upload to GitHub and publish the test build

1. Extract Orkestryn-GitHub.zip to a fresh folder. The repository root contains README.md, LICENSE, run.py, app/, docs/, scripts/ and .github/. Do not use the installed AgentRehearsal runtime directory.
2. In GitHub Desktop, sign in as williamf48 and add the extracted orkestryn folder as a local repository. Create a repository there if prompted; preserve the existing files. Review your author name and verified/no-reply email.
3. Run `py -3 scripts/check_repository.py`, then commit the clean source. Publish as Orkestryn, with public visibility and no organization selected unless intended. If it already exists, inspect and update that repository normally. Never force-push unrelated contents.
4. Review Actions → Offline verification. The included workflow has not run on GitHub until the repository is published.
5. Run Actions → Publish verified release, or build locally with `py -3 scripts/build_release.py --output dist`. The manual release workflow publishes a source ZIP and Orkestryn.pyz from the selected commit after checks. It does not expose private runtime data or provider keys.
6. If using locally built assets, create a GitHub Release from the matching commit and attach the source ZIP and Orkestryn.pyz. Assets under dist/ are release attachments, not source files to commit.
7. Test the public source and download while signed out; launch the public download on another computer. Paste the actual release URL into Devpost's test-build field.

Optional PowerShell helper: `powershell -ExecutionPolicy RemoteSigned -File scripts/publish_github.ps1 -RepositoryUrl https://github.com/williamf48/Orkestryn.git`. It prepares a local Git index and scans staged blobs. Add -Push only when you want it to commit and push. Create the remote repository yourself first; the helper does not create GitHub repositories, store tokens or force-push.

Do not upload the outer ZIP as the repository's only file. Extract it and commit its contents. GitHub Desktop preserves .github/ and other dotfiles that manual drag-and-drop can miss. A downloadable asset alone does not satisfy the requirement for public functional source.

Suggested description: “Orkestryn (formerly Agent Rehearsal): multi-agent planning, governance and recovery with NVIDIA Nemotron through Nebius, exact approvals and reproducible safety evaluation.”

The app's main and judge demo are already included. GitHub publishes source and release downloads; it does not automatically host this Python server. See [competition requirements](competition.md) for video and free working judge-access requirements.

Official references: [adding local source](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github), [GitHub Desktop](https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-a-repository-from-your-local-computer-to-github-desktop), and [release assets](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
