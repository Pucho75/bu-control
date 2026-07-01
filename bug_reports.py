import os, sqlite3
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for, flash
from datetime import datetime

bug_reports_bp = Blueprint('bug_reports', __name__)
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@bug_reports_bp.route('/report-issue', methods=['POST'])
def report_issue():
    if 'user_id' not in session:
        return jsonify({'ok': False, 'error': 'not logged in'}), 401
    message = request.form.get('message', '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'empty message'}), 400
    page_url = request.form.get('page_url', '')
    db = get_db()
    db.execute(
        "INSERT INTO bug_reports (user_id, username, page_url, message) VALUES (?, ?, ?, ?)",
        (session.get('user_id'), session.get('username', ''), page_url, message)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True})

@bug_reports_bp.route('/admin/bug-reports')
def list_bug_reports():
    if session.get('role') not in ('CEO', 'BU_DIRECTOR'):
        flash('Accesso non autorizzato')
        return redirect(url_for('dashboard'))
    db = get_db()
    reports = db.execute(
        "SELECT * FROM bug_reports ORDER BY status = 'Fixed' ASC, created_at DESC"
    ).fetchall()
    db.close()
    return render_template('bug_reports_admin.html', reports=reports)

@bug_reports_bp.route('/admin/bug-reports/<int:report_id>/status', methods=['POST'])
def update_bug_status(report_id):
    if session.get('role') not in ('CEO', 'BU_DIRECTOR'):
        return jsonify({'ok': False}), 401
    new_status = request.form.get('status')
    if new_status not in ('Open', 'In Progress', 'Fixed'):
        return jsonify({'ok': False, 'error': 'invalid status'}), 400
    db = get_db()
    if new_status == 'Fixed':
        db.execute("UPDATE bug_reports SET status=?, resolved_at=? WHERE id=?",
                   (new_status, datetime.now().isoformat(), report_id))
    else:
        db.execute("UPDATE bug_reports SET status=?, resolved_at=NULL WHERE id=?",
                   (new_status, report_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})
