"""Teacher route handlers for CPJudge."""
import csv
import io
from pathlib import Path
import re
import zipfile
from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_

from cpjudge.auth.decorators import teacher_required
from cpjudge.models import (
    Assignment,
    AuditLog,
    Class,
    Enrollment,
    Problem,
    Result,
    Score,
    Submission,
    Test,
    User,
    db,
    utc_now,
)
from cpjudge.teacher import teacher_bp
from cpjudge.teacher.forms import (
    AssignmentForm,
    ClassForm,
    ProblemForm,
    StudentImportForm,
    TestForm,
    ZipUploadForm,
)
from cpjudge.utils.auth import generate_random_password
from cpjudge.utils.markdown import render_markdown


def slugify(text: str) -> str:
    """Generates a clean URL slug from title."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-")


# ==============================================================================
# DASHBOARD
# ==============================================================================

@teacher_bp.route("/dashboard")
@teacher_required
def dashboard():
    """Teacher dashboard providing overview metrics and quick actions."""
    total_problems = Problem.query.count()
    total_classes = Class.query.filter_by(teacher_id=current_user.id).count()
    total_assignments = Assignment.query.join(Class).filter(Class.teacher_id == current_user.id).count()
    queued_submissions = Submission.query.filter_by(status="queued").count()

    recent_submissions = (
        Submission.query.order_by(Submission.created_at.desc())
        .limit(10)
        .all()
    )
    recent_logs = (
        AuditLog.query.order_by(AuditLog.created_at.desc())
        .limit(8)
        .all()
    )

    return render_template(
        "teacher/dashboard.html",
        total_problems=total_problems,
        total_classes=total_classes,
        total_assignments=total_assignments,
        queued_submissions=queued_submissions,
        recent_submissions=recent_submissions,
        recent_logs=recent_logs,
    )


# ==============================================================================
# PROBLEMS CRUD
# ==============================================================================

@teacher_bp.route("/problems")
@teacher_required
def problems():
    """Lists all problems."""
    all_problems = Problem.query.order_by(Problem.created_at.desc()).all()
    return render_template("teacher/problems.html", problems=all_problems)


@teacher_bp.route("/problems/new", methods=["GET", "POST"])
@teacher_required
def problem_new():
    """Creates a new problem."""
    form = ProblemForm()
    if form.validate_on_submit():
        slug = form.slug.data.strip() if form.slug.data else slugify(form.title.data)
        if not slug:
            slug = "problem"

        # Ensure uniqueness of slug
        base_slug = slug
        count = 1
        while Problem.query.filter_by(slug=slug).first() is not None:
            slug = f"{base_slug}-{count}"
            count += 1

        problem = Problem(
            title=form.title.data.strip(),
            slug=slug,
            statement=form.statement.data,
            time_limit=form.time_limit.data,
            memory_limit=form.memory_limit.data,
            output_limit_bytes=int(form.output_limit_mb.data * 1024 * 1024),
            checker_type=form.checker_type.data,
            float_tolerance=form.float_tolerance.data or 1e-6,
            checker_code=form.checker_code.data if form.checker_type.data == "custom" else None,
            scoring_mode=form.scoring_mode.data,
            created_by_id=current_user.id,
            tests_version=1,
        )
        db.session.add(problem)
        db.session.flush()

        AuditLog.log(
            action="create_problem",
            user_id=current_user.id,
            target_type="problem",
            target_id=problem.id,
            details={"title": problem.title, "slug": problem.slug},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        flash(f"Problem '{problem.title}' created successfully! Now add test cases.", "success")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

    return render_template("teacher/problem_form.html", form=form, title="Create Problem")


@teacher_bp.route("/problems/<int:problem_id>/edit", methods=["GET", "POST"])
@teacher_required
def problem_edit(problem_id: int):
    """Edits an existing problem."""
    problem = db.session.get(Problem, problem_id)
    if not problem:
        abort(404)

    form = ProblemForm(obj=problem)
    if form.validate_on_submit():
        problem.title = form.title.data.strip()
        if form.slug.data and form.slug.data.strip() != problem.slug:
            existing = Problem.query.filter_by(slug=form.slug.data.strip()).first()
            if existing and existing.id != problem.id:
                flash("Slug is already in use by another problem.", "danger")
                return render_template("teacher/problem_form.html", form=form, title="Edit Problem", problem=problem)
            problem.slug = form.slug.data.strip()

        problem.statement = form.statement.data
        problem.time_limit = form.time_limit.data
        problem.memory_limit = form.memory_limit.data
        problem.output_limit_bytes = int(form.output_limit_mb.data * 1024 * 1024)
        problem.checker_type = form.checker_type.data
        problem.float_tolerance = form.float_tolerance.data or 1e-6
        problem.checker_code = form.checker_code.data if form.checker_type.data == "custom" else None
        problem.scoring_mode = form.scoring_mode.data

        AuditLog.log(
            action="edit_problem",
            user_id=current_user.id,
            target_type="problem",
            target_id=problem.id,
            details={"title": problem.title},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        flash(f"Problem '{problem.title}' updated successfully.", "success")
        return redirect(url_for("teacher.problems"))

    # Populate output_limit_mb
    form.output_limit_mb.data = int(problem.output_limit_bytes / (1024 * 1024))
    return render_template("teacher/problem_form.html", form=form, title="Edit Problem", problem=problem)


@teacher_bp.route("/problems/<int:problem_id>/delete", methods=["POST"])
@teacher_required
def problem_delete(problem_id: int):
    """Deletes a problem and its associated tests and assignments."""
    problem = db.session.get(Problem, problem_id)
    if not problem:
        abort(404)

    title = problem.title
    AuditLog.log(
        action="delete_problem",
        user_id=current_user.id,
        target_type="problem",
        target_id=problem.id,
        details={"title": title},
        ip_address=request.remote_addr,
    )
    db.session.delete(problem)
    db.session.commit()
    flash(f"Problem '{title}' has been deleted.", "info")
    return redirect(url_for("teacher.problems"))


# ==============================================================================
# TESTS & ZIP UPLOAD
# ==============================================================================

@teacher_bp.route("/problems/<int:problem_id>/tests", methods=["GET", "POST"])
@teacher_required
def problem_tests(problem_id: int):
    """Manages test cases for a problem, including manual add and ZIP upload."""
    problem = db.session.get(Problem, problem_id)
    if not problem:
        abort(404)

    add_form = TestForm()
    zip_form = ZipUploadForm()

    if add_form.validate_on_submit() and "input_data" in request.form:
        next_order = len(problem.tests) + 1
        test = Test(
            problem_id=problem.id,
            order_num=add_form.order_num.data or next_order,
            input_data=add_form.input_data.data,
            expected_output=add_form.expected_output.data,
            marks=add_form.marks.data,
            is_hidden=add_form.is_hidden.data,
            subtask_id=add_form.subtask_id.data.strip() if add_form.subtask_id.data else None,
            explanation=add_form.explanation.data.strip() if add_form.explanation.data else None,
        )
        db.session.add(test)
        problem.increment_tests_version()

        AuditLog.log(
            action="create_test",
            user_id=current_user.id,
            target_type="test",
            target_id=test.id,
            details={"problem_id": problem.id, "tests_version": problem.tests_version},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Test case added successfully! Submissions flagged stale until rejudged.", "success")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

    return render_template(
        "teacher/problem_tests.html",
        problem=problem,
        add_form=add_form,
        zip_form=zip_form,
    )


@teacher_bp.route("/problems/<int:problem_id>/tests/<int:test_id>/edit", methods=["GET", "POST"])
@teacher_required
def test_edit(problem_id: int, test_id: int):
    """Edits a single test case."""
    problem = db.session.get(Problem, problem_id)
    test = db.session.get(Test, test_id)
    if not problem or not test or test.problem_id != problem.id:
        abort(404)

    form = TestForm(obj=test)
    if form.validate_on_submit():
        test.order_num = form.order_num.data
        test.input_data = form.input_data.data
        test.expected_output = form.expected_output.data
        test.marks = form.marks.data
        test.is_hidden = form.is_hidden.data
        test.subtask_id = form.subtask_id.data.strip() if form.subtask_id.data else None
        test.explanation = form.explanation.data.strip() if form.explanation.data else None

        problem.increment_tests_version()
        AuditLog.log(
            action="edit_test",
            user_id=current_user.id,
            target_type="test",
            target_id=test.id,
            details={"problem_id": problem.id, "tests_version": problem.tests_version},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Test case updated successfully! Tests version bumped.", "success")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

    return render_template("teacher/test_form.html", form=form, problem=problem, test=test)


@teacher_bp.route("/problems/<int:problem_id>/tests/<int:test_id>/delete", methods=["POST"])
@teacher_required
def test_delete(problem_id: int, test_id: int):
    """Deletes a single test case."""
    problem = db.session.get(Problem, problem_id)
    test = db.session.get(Test, test_id)
    if not problem or not test or test.problem_id != problem.id:
        abort(404)

    db.session.delete(test)
    problem.increment_tests_version()

    AuditLog.log(
        action="delete_test",
        user_id=current_user.id,
        target_type="test",
        target_id=test_id,
        details={"problem_id": problem.id, "tests_version": problem.tests_version},
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Test case deleted. Tests version bumped.", "info")
    return redirect(url_for("teacher.problem_tests", problem_id=problem.id))


@teacher_bp.route("/problems/<int:problem_id>/tests/upload-zip", methods=["POST"])
@teacher_required
def tests_upload_zip(problem_id: int):
    """Extracts test cases (.in paired with .ans/.out) from an uploaded ZIP archive."""
    problem = db.session.get(Problem, problem_id)
    if not problem:
        abort(404)

    form = ZipUploadForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

    zip_file = form.zip_file.data
    default_marks = form.default_marks.data
    visibility_mode = form.visibility_mode.data
    overwrite = form.overwrite.data

    try:
        with zipfile.ZipFile(zip_file.stream) as zf:
            # Map files by base stem
            file_map = {}
            for filename in zf.namelist():
                # Prevent directory traversal
                if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
                    continue

                base = Path(filename).name
                if not base or base.startswith("."):
                    continue

                stem = Path(base).stem
                ext = Path(base).suffix.lower()

                if ext in (".in", ".ans", ".out"):
                    try:
                        content = zf.read(filename).decode("utf-8", errors="replace")
                        file_map.setdefault(stem, {})[ext] = content
                    except Exception:
                        pass

            # Match (.in) with (.ans or .out)
            matched_pairs = []
            for stem in sorted(file_map.keys()):
                in_content = file_map[stem].get(".in")
                ans_content = file_map[stem].get(".ans") or file_map[stem].get(".out")

                if in_content is not None and ans_content is not None:
                    # Determine visibility
                    if visibility_mode == "all_visible":
                        hidden = False
                    elif visibility_mode == "all_hidden":
                        hidden = True
                    else:
                        hidden = "hidden" in stem.lower()

                    matched_pairs.append((stem, in_content, ans_content, hidden))

            if not matched_pairs:
                flash("No valid test pairs (*.in with *.ans or *.out) found in the ZIP archive.", "warning")
                return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

            if overwrite:
                Test.query.filter_by(problem_id=problem.id).delete()

            start_order = len(problem.tests) + 1 if not overwrite else 1
            for idx, (stem, in_data, ans_data, hidden) in enumerate(matched_pairs):
                test = Test(
                    problem_id=problem.id,
                    order_num=start_order + idx,
                    input_data=in_data,
                    expected_output=ans_data,
                    marks=default_marks,
                    is_hidden=hidden,
                    subtask_id=None,
                    explanation=f"Imported from {stem}",
                )
                db.session.add(test)

            problem.increment_tests_version()
            AuditLog.log(
                action="upload_tests_zip",
                user_id=current_user.id,
                target_type="problem",
                target_id=problem.id,
                details={
                    "imported_count": len(matched_pairs),
                    "overwrite": overwrite,
                    "tests_version": problem.tests_version,
                },
                ip_address=request.remote_addr,
            )
            db.session.commit()

            flash(f"Successfully imported {len(matched_pairs)} test cases from ZIP! Problem tests version is now v{problem.tests_version}.", "success")
            return redirect(url_for("teacher.problem_tests", problem_id=problem.id))

    except zipfile.BadZipFile:
        flash("Corrupted or invalid ZIP archive.", "danger")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))
    except Exception as e:
        flash(f"Error extracting ZIP file: {e}", "danger")
        return redirect(url_for("teacher.problem_tests", problem_id=problem.id))


# ==============================================================================
# CLASSES & CSV STUDENT IMPORT
# ==============================================================================

@teacher_bp.route("/classes")
@teacher_required
def classes():
    """Lists teacher's classes."""
    all_classes = Class.query.filter_by(teacher_id=current_user.id).order_by(Class.created_at.desc()).all()
    return render_template("teacher/classes.html", classes=all_classes)


@teacher_bp.route("/classes/new", methods=["GET", "POST"])
@teacher_required
def class_new():
    """Creates a new academic class."""
    form = ClassForm()
    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        if Class.query.filter_by(code=code).first() is not None:
            flash(f"A class with code '{code}' already exists.", "danger")
            return render_template("teacher/class_form.html", form=form, title="Create Class")

        classroom = Class(
            name=form.name.data.strip(),
            code=code,
            description=form.description.data.strip() if form.description.data else None,
            teacher_id=current_user.id,
        )
        db.session.add(classroom)
        db.session.flush()

        AuditLog.log(
            action="create_class",
            user_id=current_user.id,
            target_type="class",
            target_id=classroom.id,
            details={"name": classroom.name, "code": classroom.code},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash(f"Class '{classroom.name}' ({classroom.code}) created successfully!", "success")
        return redirect(url_for("teacher.class_detail", class_id=classroom.id))

    return render_template("teacher/class_form.html", form=form, title="Create Class")


@teacher_bp.route("/classes/<int:class_id>")
@teacher_required
def class_detail(class_id: int):
    """Displays class details, roster of enrolled students, and assignments."""
    classroom = db.session.get(Class, class_id)
    if not classroom:
        abort(404)

    import_form = StudentImportForm()
    return render_template("teacher/class_detail.html", classroom=classroom, import_form=import_form)


@teacher_bp.route("/classes/<int:class_id>/edit", methods=["GET", "POST"])
@teacher_required
def class_edit(class_id: int):
    """Edits a class."""
    classroom = db.session.get(Class, class_id)
    if not classroom:
        abort(404)

    form = ClassForm(obj=classroom)
    if form.validate_on_submit():
        code = form.code.data.strip().upper()
        existing = Class.query.filter_by(code=code).first()
        if existing and existing.id != classroom.id:
            flash(f"Class code '{code}' is already used by another class.", "danger")
            return render_template("teacher/class_form.html", form=form, title="Edit Class", classroom=classroom)

        classroom.name = form.name.data.strip()
        classroom.code = code
        classroom.description = form.description.data.strip() if form.description.data else None

        AuditLog.log(
            action="edit_class",
            user_id=current_user.id,
            target_type="class",
            target_id=classroom.id,
            details={"name": classroom.name, "code": classroom.code},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Class updated successfully.", "success")
        return redirect(url_for("teacher.class_detail", class_id=classroom.id))

    return render_template("teacher/class_form.html", form=form, title="Edit Class", classroom=classroom)


@teacher_bp.route("/classes/<int:class_id>/delete", methods=["POST"])
@teacher_required
def class_delete(class_id: int):
    """Deletes a class."""
    classroom = db.session.get(Class, class_id)
    if not classroom:
        abort(404)

    name = classroom.name
    AuditLog.log(
        action="delete_class",
        user_id=current_user.id,
        target_type="class",
        target_id=classroom.id,
        details={"name": name, "code": classroom.code},
        ip_address=request.remote_addr,
    )
    db.session.delete(classroom)
    db.session.commit()
    flash(f"Class '{name}' deleted.", "info")
    return redirect(url_for("teacher.classes"))


@teacher_bp.route("/classes/<int:class_id>/import-students", methods=["GET", "POST"])
@teacher_required
def class_import_students(class_id: int):
    """Bulk-imports students from CSV, auto-generates initial passwords, and forces password change."""
    classroom = db.session.get(Class, class_id)
    if not classroom:
        abort(404)

    form = StudentImportForm()
    if request.method == "POST":
        content = ""
        if form.csv_file.data:
            content = form.csv_file.data.read().decode("utf-8", errors="replace")
        elif form.csv_text.data:
            content = form.csv_text.data

        if not content.strip():
            flash("Please provide a CSV file or paste CSV text.", "danger")
            return redirect(url_for("teacher.class_detail", class_id=classroom.id))

        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            flash("The CSV data appears to be empty.", "warning")
            return redirect(url_for("teacher.class_detail", class_id=classroom.id))

        # Check for header
        header = [col.strip().lower() for col in rows[0]]
        roll_idx = 0
        name_idx = 1
        has_header = False

        for i, h in enumerate(header):
            if "roll" in h:
                roll_idx = i
                has_header = True
            elif "name" in h:
                name_idx = i
                has_header = True

        data_rows = rows[1:] if has_header else rows
        imported_credentials = []
        already_enrolled = 0

        for row in data_rows:
            if not row or len(row) <= max(roll_idx, name_idx):
                continue
            rno = row[roll_idx].strip()
            name = row[name_idx].strip()
            if not rno or not name:
                continue

            username = rno.lower()
            student = User.query.filter(
                (User.roll_no == rno) | (User.username == username)
            ).first()

            initial_password = None
            if not student:
                initial_password = generate_random_password(10)
                student = User(
                    username=username,
                    roll_no=rno,
                    name=name,
                    role="student",
                    force_password_change=True,  # Force password change on first login!
                )
                student.set_password(initial_password)
                db.session.add(student)
                db.session.flush()

            # Enroll in class if not enrolled
            enrollment = Enrollment.query.filter_by(user_id=student.id, class_id=classroom.id).first()
            if not enrollment:
                db.session.add(Enrollment(user_id=student.id, class_id=classroom.id))
                status = "New Account Created" if initial_password else "Enrolled Existing Student"
            else:
                already_enrolled += 1
                status = "Already Enrolled"

            imported_credentials.append({
                "roll_no": rno,
                "name": name,
                "username": student.username,
                "initial_password": initial_password or "(unchanged)",
                "status": status,
            })

        AuditLog.log(
            action="bulk_import_students",
            user_id=current_user.id,
            target_type="class",
            target_id=classroom.id,
            details={"imported_count": len(imported_credentials), "class_code": classroom.code},
            ip_address=request.remote_addr,
        )
        db.session.commit()

        return render_template(
            "teacher/student_import_result.html",
            classroom=classroom,
            credentials=imported_credentials,
        )

    return redirect(url_for("teacher.class_detail", class_id=classroom.id))


# ==============================================================================
# ASSIGNMENTS
# ==============================================================================

@teacher_bp.route("/assignments")
@teacher_required
def assignments():
    """Lists assignments."""
    all_assignments = (
        Assignment.query.join(Class)
        .filter(Class.teacher_id == current_user.id)
        .order_by(Assignment.due_date.desc())
        .all()
    )
    return render_template("teacher/assignments.html", assignments=all_assignments)


@teacher_bp.route("/assignments/new", methods=["GET", "POST"])
@teacher_required
def assignment_new():
    """Creates a new assignment."""
    form = AssignmentForm()
    teacher_classes = Class.query.filter_by(teacher_id=current_user.id).all()
    all_problems = Problem.query.order_by(Problem.title.asc()).all()

    form.class_id.choices = [(c.id, f"{c.name} ({c.code})") for c in teacher_classes]
    form.problem_id.choices = [(p.id, f"{p.title} (v{p.tests_version})") for p in all_problems]

    if form.validate_on_submit():
        # Check uniqueness of class + problem
        existing = Assignment.query.filter_by(class_id=form.class_id.data, problem_id=form.problem_id.data).first()
        if existing:
            flash("This problem is already assigned to this class.", "danger")
            return render_template("teacher/assignment_form.html", form=form, title="Create Assignment")

        assignment = Assignment(
            class_id=form.class_id.data,
            problem_id=form.problem_id.data,
            title=form.title.data.strip(),
            open_date=form.open_date.data,
            due_date=form.due_date.data,
            max_attempts=form.max_attempts.data,
            late_penalty_percent=form.late_penalty_percent.data,
            allow_late=form.allow_late.data,
        )
        db.session.add(assignment)
        db.session.flush()

        AuditLog.log(
            action="create_assignment",
            user_id=current_user.id,
            target_type="assignment",
            target_id=assignment.id,
            details={"title": assignment.title, "class_id": assignment.class_id, "problem_id": assignment.problem_id},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash(f"Assignment '{assignment.title}' created successfully!", "success")
        return redirect(url_for("teacher.assignments"))

    return render_template("teacher/assignment_form.html", form=form, title="Create Assignment")


@teacher_bp.route("/assignments/<int:assignment_id>/edit", methods=["GET", "POST"])
@teacher_required
def assignment_edit(assignment_id: int):
    """Edits an assignment."""
    assignment = db.session.get(Assignment, assignment_id)
    if not assignment:
        abort(404)

    form = AssignmentForm(obj=assignment)
    teacher_classes = Class.query.filter_by(teacher_id=current_user.id).all()
    all_problems = Problem.query.order_by(Problem.title.asc()).all()

    form.class_id.choices = [(c.id, f"{c.name} ({c.code})") for c in teacher_classes]
    form.problem_id.choices = [(p.id, f"{p.title} (v{p.tests_version})") for p in all_problems]

    if form.validate_on_submit():
        assignment.class_id = form.class_id.data
        assignment.problem_id = form.problem_id.data
        assignment.title = form.title.data.strip()
        assignment.open_date = form.open_date.data
        assignment.due_date = form.due_date.data
        assignment.max_attempts = form.max_attempts.data
        assignment.late_penalty_percent = form.late_penalty_percent.data
        assignment.allow_late = form.allow_late.data

        AuditLog.log(
            action="edit_assignment",
            user_id=current_user.id,
            target_type="assignment",
            target_id=assignment.id,
            details={"title": assignment.title},
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Assignment updated successfully.", "success")
        return redirect(url_for("teacher.assignments"))

    return render_template("teacher/assignment_form.html", form=form, title="Edit Assignment", assignment=assignment)


@teacher_bp.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
@teacher_required
def assignment_delete(assignment_id: int):
    """Deletes an assignment."""
    assignment = db.session.get(Assignment, assignment_id)
    if not assignment:
        abort(404)

    title = assignment.title
    AuditLog.log(
        action="delete_assignment",
        user_id=current_user.id,
        target_type="assignment",
        target_id=assignment.id,
        details={"title": title},
        ip_address=request.remote_addr,
    )
    db.session.delete(assignment)
    db.session.commit()
    flash(f"Assignment '{title}' deleted.", "info")
    return redirect(url_for("teacher.assignments"))


# ==============================================================================
# SUBMISSIONS & REJUDGE
# ==============================================================================

@teacher_bp.route("/submissions")
@teacher_required
def submissions():
    """Lists all submissions with search and filter capabilities."""
    query = Submission.query

    problem_id = request.args.get("problem_id", type=int)
    class_id = request.args.get("class_id", type=int)
    verdict = request.args.get("verdict")
    status = request.args.get("status")
    search = request.args.get("search", "").strip()
    stale_only = request.args.get("stale") == "1"

    if problem_id:
        query = query.filter(Submission.problem_id == problem_id)
    if class_id:
        query = query.join(Assignment).filter(Assignment.class_id == class_id)
    if verdict:
        query = query.filter(Submission.verdict == verdict)
    if status:
        query = query.filter(Submission.status == status)
    if search:
        query = query.join(User).filter(
            or_(
                User.name.ilike(f"%{search}%"),
                User.username.ilike(f"%{search}%"),
                User.roll_no.ilike(f"%{search}%"),
            )
        )

    all_subs = query.order_by(Submission.created_at.desc()).all()

    if stale_only:
        all_subs = [s for s in all_subs if s.is_stale]

    problems_list = Problem.query.order_by(Problem.title.asc()).all()
    classes_list = Class.query.filter_by(teacher_id=current_user.id).all()

    return render_template(
        "teacher/submissions.html",
        submissions=all_subs,
        problems=problems_list,
        classes=classes_list,
        selected_problem=problem_id,
        selected_class=class_id,
        selected_verdict=verdict,
        selected_status=status,
        search=search,
        stale_only=stale_only,
    )


@teacher_bp.route("/submissions/<int:submission_id>")
@teacher_required
def submission_detail(submission_id: int):
    """Detailed view of a submission including source code and teacher view of all tests."""
    submission = db.session.get(Submission, submission_id)
    if not submission:
        abort(404)

    return render_template("teacher/submission_detail.html", submission=submission)


@teacher_bp.route("/submissions/<int:submission_id>/rejudge", methods=["POST"])
@teacher_required
def rejudge_submission(submission_id: int):
    """Rejudges a single submission by resetting status to 'queued'."""
    submission = db.session.get(Submission, submission_id)
    if not submission:
        abort(404)

    submission.status = "queued"
    submission.verdict = None
    submission.compile_error = None
    submission.tests_version = submission.problem.tests_version

    if submission.score:
        submission.score.is_stale = False

    AuditLog.log(
        action="rejudge_submission",
        user_id=current_user.id,
        target_type="submission",
        target_id=submission.id,
        details={"problem_id": submission.problem_id, "user_id": submission.user_id},
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash(f"Submission #{submission.id} has been queued for rejudging.", "info")
    return redirect(url_for("teacher.submission_detail", submission_id=submission.id))


@teacher_bp.route("/problems/<int:problem_id>/rejudge-all", methods=["POST"])
@teacher_required
def rejudge_problem(problem_id: int):
    """Rejudges all submissions for a problem (e.g. after tests are updated)."""
    problem = db.session.get(Problem, problem_id)
    if not problem:
        abort(404)

    subs = Submission.query.filter_by(problem_id=problem.id).all()
    for s in subs:
        s.status = "queued"
        s.verdict = None
        s.compile_error = None
        s.tests_version = problem.tests_version
        if s.score:
            s.score.is_stale = False

    AuditLog.log(
        action="rejudge_problem",
        user_id=current_user.id,
        target_type="problem",
        target_id=problem.id,
        details={"submission_count": len(subs), "tests_version": problem.tests_version},
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash(f"Queued all {len(subs)} submissions of '{problem.title}' for rejudging.", "info")
    return redirect(url_for("teacher.submissions", problem_id=problem.id))


# ==============================================================================
# AUDIT LOGS
# ==============================================================================

@teacher_bp.route("/audit-logs")
@teacher_required
def audit_logs():
    """Lists audit logs."""
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(150).all()
    return render_template("teacher/audit_logs.html", logs=logs)
