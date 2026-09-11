# Method

A panel follows the same firms through successive editions of a public filing. The SEC republishes its roster of registered investment advisers every month, and each adviser in it carries a CRD number that stays the same from one file to the next. Fix a group of firms in one month's file, find each of them again in later files, and a stack of snapshots becomes a record of what happened to particular firms: which left the SEC's rolls, which changed, and which stayed exactly where they were.

Most of the work is in not being fooled by the files. Every piece here follows the same rules:

- Every figure traces to a line of code in the public repository that printed it.
- Columns are resolved against each file's real header row, and their values are checked rather than assumed. A field that reads like a website address can turn out to be a yes/no flag.
- Every derived cut has a cross-check that fails loudly. Firms entered minus firms exited must equal the net change, and a diff of two rosters is checked against each firm's own SEC status date.
- Each file is dated by the latest filing it contains, never by the date in its name.
- Every source file is checksummed and its download URL recorded. The archive is listed on the [Data](/data) page.

The rest of this page is the method appendix for the one panel published so far, [the Form ADV new-registrant panel](/adv), adapted from [METHOD.md](https://github.com/ronan-schultz/filingstrail/blob/master/adv-report/METHOD.md) in the repository. Its sources and checksums are at [the end of the report](/adv#sources).
