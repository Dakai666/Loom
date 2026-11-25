"""
Contract: the classifier is less narrow, and DK sees it work (#690 S6c).

On 2026-11-25 DK clicked five confirms while Loom Agent commented on
#702. The classifier ran, but:

1. ``gh issue view`` went to the model, which always abstains on a
   network read whose effect the packet marks unknown.
2. ``git ls-files … .env`` was refused as a hidden path, though it only
   lists names.
3. ``test ! -e …`` had no rule.
4. A workspace document write: the model abstained (unchanged here).
5. ``gh issue comment``: a post, asks by design (unchanged here).

And the confirm prompt never said the classifier had looked.

Now: gh reads and ordinary read-only probes are allowed by rule, names-only
listings ignore hidden and credential-like names, and the confirm prompt
carries the classifier's reason. Reading secrets' contents, writing and
posting still ask.
"""

from __future__ import annotations

import pytest

from loom.core.harness.approval import describe_note
from loom.core.harness.scope import ConfirmDecision

from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    Env,
    FakeModel,
    _no_network,
    ledger,
)


# Credentials in formats the name and content checks miss: base64 ``auth``,
# colon-separated passwords, short netrc passwords (#703 review).
CREDENTIAL_DOTFILES = {
    ".docker/config.json": '{"auths":{"r.io":{"auth":"dXNlcjpwYXNzd29yZA=="}}}\n',
    ".dockercfg": '{"r.io":{"auth":"dXNlcjpwYXNzd29yZA==","email":"a@b.c"}}\n',
    ".pgpass": "db.io:5432:app:admin:s3cr3t-p4ssw0rd\n",
    ".kube/config": "users:\n- name: a\n  user:\n    client-key-data: TFMwdExTMUNSVWRKVGlCU1UwRQ==\n",
    ".netrc": "machine api.example.com login me password hunter2\n",
    ".npmrc": "_auth=dXNlcjpwYXNzd29yZA==\n",
    ".yarnrc.yml": "npmAuthIdent: user:pass\n",
}


def _env(tmp_path, ledger, **kw) -> Env:
    env = Env(tmp_path, ledger, **kw)
    for rel, text in {
        "news/a.md": "今天的新聞。\n第二行。\n",
        "outputs/doc/.keep": "",
        "data/x.json": '{"a": 1}\n',
        ".github/workflows/ci.yml": "on: push\n",
        ".git/config": "[remote]\n",
        ".env": "OPENAI=sk-proj-abcdefghijklmnopqrstuvwxyz\n",
        ".gitignore": "*.pyc\n",
        ".editorconfig": "root = true\n",
        **CREDENTIAL_DOTFILES,
    }.items():
        f = env.ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return env


async def _ask(env: Env, command: str, origin: str = "discord") -> bool:
    env.confirm.reset_mock()
    await env.run("run_bash", {"command": command}, origin=origin)
    return env.confirm.await_count > 0


# ---------------------------------------------------------------------------
# Today's confirms
# ---------------------------------------------------------------------------


class TestTodaysConfirms:
    @pytest.mark.parametrize("command", [
        # 1 — 14:54
        "gh issue view 702 --repo Dakai666/Loom --json number,title,body,comments,url",
        # 2 — 14:55
        "pwd; git ls-files --others --exclude-standard --directory; "
        "git ls-files --others --ignored --exclude-standard --directory; "
        "git ls-files skills autonomy/circadian news outputs SOUL.md Agent.md loom.toml .env "
        "| head -100",
        # 3 — 15:06
        "ls -ld outputs/doc; test ! -e outputs/doc/issue-702-sisi-supplement-20261125.md "
        "&& echo 'comment file: available'; gh issue view 702 --repo Dakai666/Loom "
        "--json title,body,comments",
        # after the post — 15:09
        "gh api repos/Dakai666/Loom/issues/comments/7155542256 --jq '{html_url,body}'",
    ])
    async def test_reads_no_longer_ask_or_reach_the_model(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'))
        assert not await _ask(env, command)
        assert env.model.requests == [] and len(env.ran) == 1

    async def test_the_post_still_asks(self, tmp_path, ledger):
        # 5 — 15:08
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert await _ask(env, "gh issue comment 702 --repo Dakai666/Loom --body-file x.md")
        assert env.model.requests == []


# ---------------------------------------------------------------------------
# Ordinary read-only probes
# ---------------------------------------------------------------------------


class TestWiderReads:
    @pytest.mark.parametrize("command", [
        "gh pr view 701 --json title", "gh pr list --state open", "gh pr checks 701",
        "gh run list -L 5", "gh search issues approval", "gh api repos/x/y/pulls --paginate",
        "gh api -X GET repos/x/y/issues", "gh issue view 1 --jq .title",
        "sed -n 1,40p news/a.md", "sed -n '$p' news/a.md", "sed -n '1p;3,5p' news/a.md",
        "head -n 3 news/a.md | sed -n 2p",
        "jq .a data/x.json", "jq -r '.a' data/x.json", "cat data/x.json | jq -c .",
        "jq --arg k a '.[$k]' data/x.json",
        "which python3", "type git", "command -v gh", "uname -a", "whoami",
        "test -d news", "[ -f news/a.md ]", "[ ! -e news/zzz ] && echo free",
        "sort news/a.md", "sort news/a.md | uniq -c", "uniq news/a.md", "tr a-z A-Z",
        "tree news", "realpath news/a.md", "basename news/a.md", "dirname news/a.md",
        "printf '%s\\n' hi", "true", "cat .github/workflows/ci.yml",
        "cd .github && cat workflows/ci.yml", "cat .gitignore", "head .editorconfig",
        "jq .env data/x.json", "jq '.a.env' data/x.json", "gh issue view 1 --jq .body.env",
        "ls -la .git", "ls .env", "git ls-files .env", "git status --short .github",
        "find . -name '*.md'", "cd news && cat a.md",
    ])
    async def test_read_runs_without_model_or_dk(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'))
        assert not await _ask(env, command), command
        assert env.model.requests == []

    async def test_gh_read_needs_no_model_unattended_either(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'))
        r = await env.run("run_bash", {"command": "gh issue view 650"}, origin="chime")
        assert r.success and env.model.requests == []


class TestStillAsks:
    @pytest.mark.parametrize("command", [
        # secrets' contents, wherever they hide
        "cat .env", "head .git/config", "cd .git && cat config", "git show HEAD:.env",
        "sed -n 1p .env", "jq . .env", "sort .env", "tree -o out.txt news",
        "jq -n env.GITHUB_TOKEN", "jq -n '$ENV.HOME'", "gh api user --jq env.GH_TOKEN",
        "gh issue view 1 -q '$ENV'", "jq 'import \"data\" as $d; $d' data/x.json",
        "cat .GIT/config",
        # writes and program runs dressed as reads
        "sed -i s/a/b/ news/a.md", "sed -n 1w/tmp/x news/a.md", "sed -f s.sed news/a.md",
        "sed -e 1p news/a.md", "sed s/a/b/ news/a.md", "sed -n '1p;e rm x' news/a.md",
        "jq -f prog.jq data/x.json", "jq -rf prog.jq data/x.json",
        "jq --rawfile k .env . data/x.json", "jq -L /tmp 'import \"x\" as x; .' data/x.json",
        "sort -o out.txt news/a.md", "uniq news/a.md out.txt", "command rm -rf news",
        "[ -e news/a.md", "test -e /etc/passwd", "ls ../outside", "cd ..",
        "realpath /etc/passwd",
        # the browser and anything that is not a gh read
        "gh issue view 1 --web", "gh pr create --fill", "gh api -X PATCH repos/x/y",
        "gh api graphql -f query=x", "gh auth token",
    ])
    async def test_asks_dk_without_the_model(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert await _ask(env, command), command
        assert env.model.requests == []


class TestCredentialDotfiles:
    """Master asked for these; S6c's first cut let content readers open any
    non-.git hidden file and these slipped past both checks (#703 review).
    Only the project-config allowlist opens for content now."""

    @pytest.mark.parametrize("path", sorted(CREDENTIAL_DOTFILES))
    @pytest.mark.parametrize("reader", ["cat {}", "head -n 5 {}", "sed -n 1p {}", "sort {}"])
    async def test_attended_read_asks(self, tmp_path, ledger, path, reader):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert await _ask(env, reader.format(path))
        assert env.model.requests == []

    @pytest.mark.parametrize("path", sorted(CREDENTIAL_DOTFILES))
    async def test_unattended_read_is_skipped(self, tmp_path, ledger, path):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        r = await env.run("run_bash", {"command": f"cat {path}"}, origin="chime")
        assert not r.success and r.metadata.get("unattended_skip") is True
        assert env.ran == [] and env.model.requests == []

    @pytest.mark.parametrize("command", [
        "cd .docker && cat config.json", "cd .kube && head config",
        "cat .github/../.pgpass", "cat .vscode/.env",
    ])
    async def test_no_way_around_the_allowlist(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"accept"}'))
        assert await _ask(env, command)

    @pytest.mark.parametrize("command", ["ls -la .docker", "test -f .pgpass", "git ls-files .npmrc"])
    async def test_listing_their_names_still_does_not_ask(self, tmp_path, ledger, command):
        env = _env(tmp_path, ledger)
        assert not await _ask(env, command)


# ---------------------------------------------------------------------------
# The confirm prompt says why the classifier left it to DK
# ---------------------------------------------------------------------------


class TestConfirmShowsClassifier:
    async def test_model_abstain_is_on_the_confirm(self, tmp_path, ledger):
        from loom.core.session_support import (
            _format_scope_panel, _format_scope_summary_plain, classifier_line,
        )
        env = _env(tmp_path, ledger, model=FakeModel('{"verdict":"abstain"}'))
        assert await _ask(env, "curl -s https://example.com/a")
        call = env.confirm.await_args.args[0]
        assert call.metadata["classifier_note"] == "model-abstain: abstain"
        # #704: the line names the mode the call was decided in.
        assert classifier_line(call) == "自動核准未放行（自理）：Luna abstained"
        assert "Luna abstained" in _format_scope_summary_plain(call)
        assert "Luna abstained" in _format_scope_panel(call)

    async def test_rule_refusal_is_on_the_confirm(self, tmp_path, ledger):
        env = _env(tmp_path, ledger)
        assert await _ask(env, "python3 tools/report.py")
        call = env.confirm.await_args.args[0]
        assert call.metadata["classifier_note"] == "rule-refuse: unknown_program"

    async def test_no_note_when_the_classifier_never_looked(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, confirm_result=ConfirmDecision.ONCE)
        assert not await _ask(env, "cat news/a.md")
        assert all("classifier_note" not in c.metadata for c in env.ran)

    @pytest.mark.parametrize("note,text", [
        ("model-abstain: abstain", "Luna abstained"),
        ("model-abstain: timeout", "Luna abstained: timeout"),
        ("model-abstain: daily review cap reached", "Luna abstained: daily review cap reached"),
        ("rule-refuse: hidden_path", "rule: hidden_path"),
        ("error", "classifier error"),
    ])
    def test_describe_note(self, note, text):
        assert describe_note(note) == text
