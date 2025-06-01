"""
Fusion 360 G-code Post-Merge Script
===================================

This script merges multiple Fusion 360 exported G-code files (.ngc) into a single G-code file for LinuxCNC or similar controllers.
It keeps only the header from the first file, concatenates all operation bodies in order, and appends a single program end block.

USAGE:
  python f360_seam.py inputdir output.ngc   # Combines all .ngc files in inputdir (ordered by last number in filename)
  python f360_seam.py file1.ngc file2.ngc ... output.ngc   # Combines these files in given order

OPTIONS:
  --no-renumber              Do NOT renumber N-lines (default: lines will be renumbered, step 10)
  --keep-all-comments        Do NOT remove any comments (default: only keep operation/tool/important comments)
  --insert-toolchange-call   Insert a toolchange subroutine call (O <toolchange> call) before every Tn M6 (default: off)
  --log logfile.txt          Write debug and info output also to logfile.txt
  --verbose                  Enable verbose debug output (prints detailed processing info to console)

DETAILS:
  - Only the header from the first file is kept
  - Each file's operation(s) are included, from the first operation comment/toolchange to before M30/%
  - The combined file ends with: M5, G53 G0 Z0., M30, %
  - The files from a directory are sorted by the LAST number before .ngc in their filename (e.g. part12.ngc → 12)
  - Any file without a number before .ngc is ignored in directory mode
  - All output lines are renumbered (N10, N20, N30, ...) unless --no-renumber is set
  - By default, only 'important' comments are kept (operation/toolchange/contour/pocket), others are stripped unless --keep-all-comments is set
  - At the end, script validates output and reports summary: toolchanges, comments kept/removed, renumbered lines, total output lines, and any validation errors.

ABOUT --insert-toolchange-call:
  There are two ways to implement safe toolchanges in LinuxCNC:

  1. **Manual subroutine call:**
     - If you want to explicitly call a toolchange subroutine in your G-code, enable `--insert-toolchange-call`.
     - This will insert a line like `O <toolchange> call` before every `Tn M6`.
     - Make sure you have a matching subroutine (with `O<toolchange> sub ... O<toolchange> endsub`) in your subroutine path.

  2. **REMAP-based toolchange (recommended for most users):**
     - If you configure REMAP in your INI, LinuxCNC will automatically call your subroutine for every `Tn M6`.
     - Your G-code only needs `Tn M6`—you do **NOT** need the `--insert-toolchange-call` option in this case.
     - Example INI config:
       ```
       [RS274NGC]
       REMAP=M6 modalgroup=6 ngc=toolchange
       ```
     - Example toolchange.ngc:
       ```
       O<toolchange> sub
       G53 G0 Z0
       G53 G0 X0 Y0
       O<toolchange> endsub
       M2
       ```

  For most workflows, option 2 (REMAP) is preferred.
  Only use --insert-toolchange-call if you need manual invocation of the subroutine outside the standard toolchange process.

  See the LinuxCNC [Remap documentation](http://linuxcnc.org/docs/html/remap/remap.html) for advanced usage.
"""
import sys
import re
from pathlib import Path
from datetime import datetime


class TeeLogger:
    def __init__(self, logfile_path=None):
        self.logfile = open(logfile_path, 'a') if logfile_path else None

    def write(self, msg):
        sys.stdout.write(msg)
        sys.stdout.flush()
        if self.logfile:
            self.logfile.write(msg)
            self.logfile.flush()

    def close(self):
        if self.logfile:
            self.logfile.close()


# Helper for counting/reporting info
class ScriptStats:
    def __init__(self):
        self.comments_kept = 0
        self.comments_removed = 0
        self.toolchanges = 0
        self.toolchange_calls = 0
        self.lines_renumbered = 0
        self.total_output_lines = 0
        self.validation_errors = []

    def summary(self):
        summary_lines = [
            f"Lines in output: {self.total_output_lines}",
            f"N-lines renumbered: {self.lines_renumbered}",
            f"Toolchanges found: {self.toolchanges}",
            f"Toolchange-calls inserted: {self.toolchange_calls}",
            f"Comments kept: {self.comments_kept}",
            f"Comments removed: {self.comments_removed}",
        ]
        if self.validation_errors:
            summary_lines.append('VALIDATION ERRORS:')
            summary_lines.extend(self.validation_errors)
        return '\n'.join(summary_lines)


def extract_header(gcode_lines, verbose=False, logger=None):
    """Extract header from G-code lines. Stop after initial setup block."""
    header_lines = []
    for gcode_line in gcode_lines:
        header_lines.append(gcode_line)
        # End of header: typically after G53 G0 Z0. or first Tn M6
        if re.match(r'^[ \t]*N20[ \t]+G53[ \t]+G0[ \t]+Z0\.', gcode_line) or re.match(r'^N\d+[ \t]+T\d+[ \t]+M6',
                                                                                      gcode_line):
            if verbose and logger:
                logger.write('[DEBUG] Header ends at: ' + gcode_line.strip() + '\n')
            break
    return header_lines


def extract_body(gcode_lines, verbose=False, logger=None):
    """Extract body from G-code lines, starting from first operation, ending before M30 or %."""
    operation_started = False
    body_lines = []
    for gcode_line in gcode_lines:
        # Start at first operation comment or toolchange (2D, 3D, etc, or Tn M6)
        if not operation_started and (
                gcode_line.strip().startswith('(') or re.match(r'^N\d+[ \t]+T\d+[ \t]+M6', gcode_line)
        ):
            operation_started = True
            if verbose and logger:
                logger.write('[DEBUG] Body starts at: ' + gcode_line.strip() + '\n')
        if operation_started:
            if 'M30' in gcode_line or gcode_line.strip() == '%' or gcode_line.strip() == 'M2':
                if verbose and logger:
                    logger.write('[DEBUG] Body ends at: ' + gcode_line.strip() + '\n')
                break  # Stop at end of program
            body_lines.append(gcode_line)
    return body_lines


def extract_number_from_filename(filename):
    """Extract the last number before the extension, to use for sorting."""
    match_number = re.search(r'(\d+)(?=\.ngc$)', filename, re.IGNORECASE)
    return int(match_number.group(1)) if match_number else -1


def get_files_sorted_by_number(directory_path, verbose=False, logger=None):
    """Return a list of .ngc files from directory, sorted by trailing number."""
    gcode_files = list(Path(directory_path).glob("*.ngc"))
    gcode_files_sorted = sorted(
        gcode_files,
        key=lambda file_path: extract_number_from_filename(file_path.name)
    )
    gcode_files_sorted = [file_path for file_path in gcode_files_sorted if
                          extract_number_from_filename(file_path.name) != -1]
    if verbose and logger:
        logger.write(f"[DEBUG] Files found in directory '{directory_path}':\n")
        for file_path in gcode_files_sorted:
            logger.write(f"  {file_path.name}\n")
    return gcode_files_sorted


def is_important_comment(line):
    stripped = line.strip().lower()
    return (
            stripped.startswith('(2d') or
            stripped.startswith('(3d') or
            stripped.startswith('(drill') or
            stripped.startswith('(contour') or
            stripped.startswith('(tool') or
            stripped.startswith('(operation') or
            stripped.startswith('(adaptive') or
            stripped.startswith('(facing') or
            stripped.startswith('(slot') or
            stripped.startswith('(bore') or
            stripped.startswith('(tap') or
            stripped.startswith('(thread') or
            stripped.startswith('(change')
    )


def clean_and_renumber(
        lines,
        stats,
        renumber=True,
        keep_all_comments=False,
        insert_toolchange_call=False,
        toolchange_subroutine='toolchange',
        start_n=10,
        step_n=10,
        verbose=False,
        logger=None,
):
    output_lines = []
    current_n = start_n
    last_was_toolcall = False
    for line in lines:
        original_line = line
        # Handle comments
        if line.strip().startswith('('):
            if keep_all_comments or is_important_comment(line):
                output_lines.append(line)
                stats.comments_kept += 1
            else:
                stats.comments_removed += 1
                if verbose and logger:
                    logger.write('[DEBUG] Comment removed: ' + line.strip() + '\n')
            continue
        # Insert toolchange call before every Tn M6
        if insert_toolchange_call and re.match(r'^N?\d*\s*T\d+\s+M6', line.lstrip()):
            if verbose and logger:
                logger.write(f'[DEBUG] Inserted O <{toolchange_subroutine}> call before: {line.strip()}\n')
            output_lines.append(f'O <{toolchange_subroutine}> call\n')
            stats.toolchange_calls += 1
            last_was_toolcall = True
        else:
            last_was_toolcall = False
        # Count toolchanges
        if re.match(r'^N?\d*\s*T\d+\s+M6', line.lstrip()):
            stats.toolchanges += 1
        # Renumber N-lines
        if renumber and line.lstrip().startswith('N'):
            code_body = re.sub(r'^N\d+[ \t]*', '', line.lstrip())
            numbered_line = f'N{current_n} {code_body}'
            output_lines.append(numbered_line)
            stats.lines_renumbered += 1
            if verbose and logger:
                logger.write(f'[DEBUG] Renumbered: {original_line.strip()} -> {numbered_line.strip()}\n')
            current_n += step_n
        else:
            output_lines.append(line)
    stats.total_output_lines = len(output_lines)
    return output_lines


def validate_output(output_lines, stats):
    errors = []
    if not output_lines[-1].strip() == '%':
        errors.append('Output does not end with %')
    m30_count = sum(1 for line in output_lines if 'M30' in line)
    if m30_count == 0:
        errors.append('No M30 found in output (should end program)')
    if m30_count > 1:
        errors.append('Multiple M30 lines found in output')
    t_toolchange_lines = sum(1 for line in output_lines if re.match(r'^N?\d*\s*T\d+\s+M6', line.lstrip()))
    if t_toolchange_lines != stats.toolchanges:
        errors.append(f'Internal error: toolchange count mismatch ({t_toolchange_lines} vs {stats.toolchanges})')
    stats.validation_errors = errors
    return errors


def main(
        input_files_list,
        output_file_path,
        verbose=False,
        renumber=True,
        keep_all_comments=False,
        insert_toolchange_call=False,
        toolchange_subroutine='toolchange',
        logfile_path=None,
):
    logger = TeeLogger(logfile_path)
    stats = ScriptStats()
    all_operation_bodies = []
    header_lines = []
    for index, input_file_path in enumerate(input_files_list):
        if verbose:
            logger.write(f"[DEBUG] Processing file {index + 1}/{len(input_files_list)}: {input_file_path}\n")
        with open(input_file_path, 'r') as gcode_file:
            gcode_lines = gcode_file.readlines()
        if index == 0:
            header_lines = extract_header(gcode_lines, verbose, logger)
            if verbose:
                logger.write('[DEBUG] Header extracted, length: ' + str(len(header_lines)) + '\n')
        body_lines = extract_body(gcode_lines, verbose, logger)
        if verbose:
            logger.write(f'[DEBUG] Body extracted from {input_file_path}, length: {len(body_lines)}\n')
            if body_lines:
                logger.write(f'[DEBUG] First body line: {body_lines[0].strip()}\n')
                logger.write(f'[DEBUG] Last body line: {body_lines[-1].strip()}\n')
        all_operation_bodies.extend(body_lines)
    processed_lines = clean_and_renumber(
        all_operation_bodies,
        stats,
        renumber=renumber,
        keep_all_comments=keep_all_comments,
        insert_toolchange_call=insert_toolchange_call,
        toolchange_subroutine=toolchange_subroutine,
        start_n=10,
        step_n=10,
        verbose=verbose,
        logger=logger,
    )
    with open(output_file_path, 'w') as output_gcode_file:
        output_gcode_file.writelines(header_lines)
        output_gcode_file.write('\n')
        output_gcode_file.writelines(processed_lines)
        output_gcode_file.write('\n')
        output_gcode_file.write('M5\n')
        output_gcode_file.write('G53 G0 Z0.\n')
        output_gcode_file.write('M30\n')
        output_gcode_file.write('%\n')
    logger.write(f'Combined to: {output_file_path}\n')
    with open(output_file_path, 'r') as out_f:
        output_lines = out_f.readlines()
    validate_output(output_lines, stats)
    logger.write("\n===== SCRIPT SUMMARY =====\n")
    logger.write(stats.summary() + "\n")
    if logger.logfile:
        logger.close()


def parse_args():
    import argparse
    argument_parser = argparse.ArgumentParser(
        description="Merge Fusion 360 .ngc files in correct order for LinuxCNC. Output is renumbered and cleaned unless options override.")
    argument_parser.add_argument('inputs', nargs='+', help="Input files or directory followed by output file name.")
    argument_parser.add_argument('--no-renumber', action='store_true',
                                 help="Do NOT renumber N-lines (default: renumber)")
    argument_parser.add_argument('--keep-all-comments', action='store_true',
                                 help="Keep all comments (default: only operation/tool comments are kept)")
    argument_parser.add_argument('--insert-toolchange-call', action='store_true',
                                 help="Insert O <toolchange> call before every toolchange (Tn M6) for LinuxCNC remap/subroutine toolchange position logic.")
    argument_parser.add_argument('--log', metavar='LOGFILE', help="Log output and debug info to logfile")
    argument_parser.add_argument('--verbose', action='store_true', help="Enable verbose debug output.")
    return argument_parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    renumber = not args.no_renumber
    keep_all_comments = args.keep_all_comments
    insert_toolchange_call = args.insert_toolchange_call
    verbose = args.verbose
    logfile_path = args.log
    if len(args.inputs) == 2 and Path(args.inputs[0]).is_dir():
        input_directory_path = args.inputs[0]
        output_file_path = args.inputs[1]
        input_files_list = get_files_sorted_by_number(input_directory_path, verbose=verbose)
        if not input_files_list:
            print("No numbered .ngc files found in directory.")
            sys.exit(2)
        if verbose:
            print("[DEBUG] Detected input order:")
            for file_path in input_files_list:
                print(f"  {file_path.name}")
    else:
        *input_files, output_file_path = args.inputs
        input_files_list = [Path(file_name) for file_name in input_files]
        if verbose:
            print(f"[DEBUG] File list mode. Input files:")
            for file_path in input_files_list:
                print(f"  {file_path}")
    main(
        input_files_list,
        output_file_path,
        verbose=verbose,
        renumber=renumber,
        keep_all_comments=keep_all_comments,
        insert_toolchange_call=insert_toolchange_call,
        logfile_path=logfile_path,
    )
