// QuickBooks Online journal-entry CSV export for the results partial.
// Bound by event delegation to <button data-action="download-qbo-csv">, so it
// works for results swapped in by HTMX. Reads the run from the
// <script type="application/json"> data blocks the partial embeds.
(function () {
  'use strict';

  function downloadQboCsv() {
    const d = JSON.parse(document.getElementById('qbo-payroll-data').textContent);
    // Money arrives as decimal strings ("0.00" is truthy, so test with Number()).
    const fmt = v => Number(v).toFixed(2);

    // Journal date = last day of pay period (YYYY-MM → MM/DD/YYYY)
    const [yr, mo] = d.payPeriod.split('-').map(Number);
    const lastDay = new Date(yr, mo, 0).getDate();
    const jDate = `${String(mo).padStart(2,'0')}/${String(lastDay).padStart(2,'0')}/${yr}`;

    const desc = d.payPeriod + ' Payroll';

    // Column order per QBO import format:
    // *JournalNo, *JournalDate, *AccountName, *Debits, *Credits, Description, Name, Currency, Location, Class
    const rows = [
      ['*JournalNo', '*JournalDate', '*AccountName', '*Debits', '*Credits', 'Description', 'Name', 'Currency', 'Location', 'Class'],
    ];

    let je = JSON.parse(document.getElementById('qbo-journal-no').textContent);
    let jeCount = 0;

    // Export exactly the entries shown on screen (server-built) so the two never diverge.
    // QBO requires "Parent:Child" account names with no space after the colon.
    for (const entry of d.journalEntries) {
      entry.lines.forEach((ln, i) => {
        const first = i === 0;
        rows.push([
          je,
          first ? jDate : '',
          ln.account.replace(/:\s+/g, ':'),
          Number(ln.debit)  ? fmt(ln.debit)  : '',
          Number(ln.credit) ? fmt(ln.credit) : '',
          ln.memo ? desc + ' - ' + ln.memo : desc,
          '',
          first ? 'USD' : '',
          '',
          '',
        ]);
      });
      je++; jeCount++;
    }

    // Build CSV string
    const csv = rows.map(r =>
      r.map(cell => {
        const s = String(cell);
        return (s.includes(',') || s.includes('"') || s.includes('\n'))
          ? '"' + s.replace(/"/g, '""') + '"'
          : s;
      }).join(',')
    ).join('\r\n');

    // Trigger download
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = 'qbo_journal_' + d.payPeriod + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    // Increment journal number, update badge. htmx.ajax issues the request from
    // <body>, so it inherits the hx-headers CSRF token like every HTMX request.
    htmx.ajax('POST', `/htmx/increment-journal-no?count=${jeCount}`,
              {target: '#je-no-badge', swap: 'outerHTML'});
  }

  document.addEventListener('click', function (event) {
    const button = event.target.closest('[data-action="download-qbo-csv"]');
    if (button) {
      event.preventDefault();
      downloadQboCsv();
    }
  });
})();
