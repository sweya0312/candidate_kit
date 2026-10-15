1. **Indexes.** Unique `employees.emp_code` and attendance (`emp_code`, `date`) indexes enforce identity and race-safe natural keys. Employee indexes support department/code listing and join-date headcounts. Attendance indexes support punch-out lookup, date-sorted listing, and date/status analytics. Monthly and correlated attendance lookups use (`emp_code`, `date`); the leaderboard scans by date.

2. **Punch-in race.** Both requests may read the employee, but each calculates the same natural attendance key. MongoDB's unique compound index serializes the inserts: one insert succeeds and returns 201; the other raises `DuplicateKeyError`, which the API maps to 409.

3. **Ties.** The leaderboard uses MongoDB competition ranking. If two employees tie at rank 10, both are returned for `limit=10`; the next employee receives rank 12. Rows tie-break by employee code.

4. **Headcount.** Department summary starts from employees, filters by `joined_on` through the month's last day, then looks up attendance. Preserving the empty lookup ensures employees with no logs still appear in the group.

5. **100x data.** I would measure with explain, then consider scheduled daily summaries for repeated analytics while retaining raw attendance as the source of truth.
