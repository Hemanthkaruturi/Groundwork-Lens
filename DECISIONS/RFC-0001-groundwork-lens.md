---
id: RFC-0001
title: GroundWork Lens v1
status: approved
requested_by: Hemanth Karuturi
owner: Hemanth Karuturi
approvers: [Hemanth Karuturi]
approved_by: [hemanth@groundwork.local]
signoffs_required: 1
---

## 1. Summary

Build GroundWork Lens as a self-hosted, open-source web application that gives leaders and product managers a plain-language view of GroundWork projects on GitHub and GitLab, lets them submit feature requests and bug reports, answer interview questions, and read, edit, and approve governed documents.

## 2. The need

Leaders read GroundWork projects mostly on a phone and should never have to open a git repository, read raw markdown, or learn GroundWork CLI commands to see what is waiting on a person, what needs approval, what is being built, and which bugs are open.

## 3. Done means

A single Docker container with a browser setup wizard starts cleanly, syncs single repos and multi-repo workspaces, displays all plain-language lists on a 360 px screen, supports GitHub and GitLab sign-in modes A/B/C, saves requests via `groundwork.py new-request`, records answers via `groundwork.py answer`, and records edits and approvals via `groundwork.py approve`.
