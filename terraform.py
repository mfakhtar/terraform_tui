#!/usr/bin/env python3

import curses
import os
import hcl2
import re
import subprocess
import tempfile
import signal
import sys
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import TerminalFormatter
from pygments.styles import get_style_by_name

selected_index = 0
selected_block_index = -1  # For selecting blocks in the second column

# Parse .tf and .tfvars files
def parse_terraform_files(directory):
    terraform_data = {}
    for file in os.listdir(directory):
        if file.endswith(".tf") or file.endswith(".tfvars"):
            file_path = os.path.join(directory, file)
            
            # Always store the raw file content first to ensure we have it even if parsing fails
            try:
                with open(file_path, 'r') as raw_f:
                    terraform_data[file + "_raw"] = raw_f.read()
            except Exception as e:
                terraform_data[file + "_raw"] = f"Error reading file: {str(e)}"
                continue  # Skip parsing if we can't even read the file
            
            # Now try to parse the file
            try:
                with open(file_path, 'r') as f:
                    content = hcl2.load(f)
                    terraform_data[file] = content
            except Exception as e:
                # If parsing fails, create a fallback structure based on regex patterns
                # This ensures we can show something in the UI even if HCL parser fails
                terraform_data[file] = extract_fallback_structure(terraform_data[file + "_raw"])
                # Log parsing error but don't let it break the TUI
                print(f"Warning: Error parsing {file}: {str(e)}")
    
    return terraform_data

# Extract a basic structure even if HCL2 parsing fails
def extract_fallback_structure(raw_content):
    """
    Extract a simplified structure of Terraform blocks when HCL2 parsing fails
    to ensure something is shown in the UI.
    """
    structure = {}
    
    # Common Terraform block types
    block_patterns = [
        r'resource\s+"([^"]+)"\s+"([^"]+)"',
        r'data\s+"([^"]+)"\s+"([^"]+)"',
        r'variable\s+"([^"]+)"',
        r'output\s+"([^"]+)"',
        r'provider\s+"?([^"\s{]+)"?',
        r'module\s+"([^"]+)"',
        r'locals\s+{',
        r'terraform\s+{',
    ]
    
    for pattern in block_patterns:
        matches = re.finditer(pattern, raw_content)
        for match in matches:
            groups = match.groups()
            if len(groups) == 2:  # resource or data block
                block_type = match.group(0).split()[0]  # resource, data, etc.
                resource_type = groups[0]
                resource_name = groups[1]
                
                if block_type not in structure:
                    structure[block_type] = {}
                
                if resource_type not in structure[block_type]:
                    structure[block_type][resource_type] = {}
                
                structure[block_type][resource_type][resource_name] = {}
            
            elif len(groups) == 1:  # variable, output, module, provider
                block_type = match.group(0).split()[0]  # variable, output, etc.
                name = groups[0]
                
                if block_type not in structure:
                    structure[block_type] = {}
                
                structure[block_type][name] = {}
            
            else:  # locals, terraform, or other blocks
                block_type = match.group(0).split()[0]
                if block_type not in structure:
                    structure[block_type] = {}
                
                # For terraform blocks, try to detect required_providers
                if block_type == "terraform":
                    # Check if this looks like a terraform block with required_providers
                    if "required_providers" in raw_content[match.start():match.start() + 200]:
                        structure[block_type]["required_providers"] = {}
                        
                # For locals blocks, try to identify individual local variables
                if block_type == "locals":
                    # Try to extract local variable names
                    locals_section = raw_content[match.start():match.start() + 500]  # Limit the search range
                    # Find local variable definitions - look for patterns like name = value
                    local_vars = re.findall(r'(\w+)\s*=\s*', locals_section)
                    for var_name in local_vars:
                        structure[block_type][var_name] = {}
    
    return structure

# Extract raw block content from file
def extract_block_content(raw_content, block_type, block_name):
    """
    Extract the content of a Terraform block from raw file content.
    Handles various block formats including:
    1. Standard brace format: block_type "block_name" { ... }
    2. Equals format: block_type "block_name" = ...
    3. Single quotes: block_type 'block_name' { ... }
    4. No quotes: block_type block_name { ... } (common for providers)
    5. Nested quotes: block_type "block_name" "subname" { ... } (for resources)
    6. Special handling for terraform.required_providers and locals
    """
    # Special handling for terraform blocks including required_providers
    if block_type == "terraform":
        # If specifically looking for required_providers inside terraform block
        if block_name == "required_providers":
            # First find the terraform block
            terraform_pattern = r'terraform\s*{(?:[^{}]|{(?:[^{}]|{[^{}]*})*})*}'
            terraform_match = re.search(terraform_pattern, raw_content, re.DOTALL)
            if terraform_match:
                terraform_content = terraform_match.group(0)
                # Now try to extract just the required_providers section
                required_providers_pattern = r'required_providers\s*{(?:[^{}]|{(?:[^{}]|{[^{}]*})*})*}'
                required_match = re.search(required_providers_pattern, terraform_content, re.DOTALL)
                if required_match:
                    # Return just the required_providers block
                    return required_match.group(0)
                # If we couldn't find the specific required_providers block, return the whole terraform block
                return terraform_content
            return "Block content not found"
        else:
            # Looking for the entire terraform block
            terraform_pattern = r'terraform\s*{(?:[^{}]|{(?:[^{}]|{[^{}]*})*})*}'
            terraform_match = re.search(terraform_pattern, raw_content, re.DOTALL)
            if terraform_match:
                return terraform_match.group(0)
            return "Block content not found"
    
    # Special handling for locals blocks
    if block_type == "locals":
        locals_pattern = r'locals\s*{(?:[^{}]|{(?:[^{}]|{[^{}]*})*})*}'
        matches = list(re.finditer(locals_pattern, raw_content, re.DOTALL))
        
        # If there's only one locals block or block_name is empty, return the first match
        if len(matches) == 1 or not block_name:
            if matches:
                return matches[0].group(0)
            return "Block content not found"
        
        # If we have a specific locals block name, try to find it
        # In this case, block_name might be a variable defined in locals
        for match in matches:
            locals_content = match.group(0)
            # Look for the specific variable in the locals block
            # Match "block_name" = ... or block_name = ... (with or without quotes)
            var_pattern = fr'["\']?{re.escape(block_name)}["\']?\s*='
            if re.search(var_pattern, locals_content):
                return locals_content
        
        # If we can't find a specific match, return the first one
        if matches:
            return matches[0].group(0)
        return "Block content not found"
    
    # Escape quotes in block_name if they exist
    escaped_block_name = block_name
    if '"' in block_name:
        escaped_block_name = block_name.replace('"', '\\"')
    
    # Define patterns for different block formats
    patterns = [
        # Resource with double quotes for both resource type and name
        fr'{block_type}\s+"{escaped_block_name}"\s*{{',
        # Resource with resource name in second position (e.g., resource "aws_s3_bucket" "bucket")
        fr'{block_type}\s+["\'][^"\']+["\']\\s+"{escaped_block_name}"\s*{{',
        # Provider or similar blocks without quotes
        fr'{block_type}\s+{escaped_block_name}\s*{{',
        # Variable or output with equals
        fr'{block_type}\s+"{escaped_block_name}"\s*=',
        # Variable without quotes with equals
        fr'{block_type}\s+{escaped_block_name}\s*=',
        # Single quote variants
        fr"{block_type}\s+'{escaped_block_name}'\s*{{",
        fr"{block_type}\s+'{escaped_block_name}'\s*="
    ]
    
    # Special case for resource blocks where block_name might be the resource type
    if block_type == "resource":
        # Try to match any resource declaration that includes block_name
        resource_patterns = [
            fr'resource\s+"{escaped_block_name}"\s+"[^"]+"\s*{{',  # resource "aws_s3_bucket" "my-bucket"
            fr'resource\s+"[^"]+"\s+"{escaped_block_name}"\s*{{'   # resource "aws_s3_bucket" "guide-tfe-es-s3"
        ]
        patterns.extend(resource_patterns)
    
    start_idx = -1
    matched_pattern = None
    matched_text = None
    
    # Try each pattern
    for pattern in patterns:
        matches = list(re.finditer(pattern, raw_content, re.MULTILINE | re.DOTALL))
        for match in matches:
            # If we find multiple matches, we need to figure out the right one
            # For now, take the first match, but this could be improved
            potential_start = match.start()
            matched_text = match.group(0)
            
            # If we already found a match earlier in the file, keep that one
            if start_idx == -1 or potential_start < start_idx:
                start_idx = potential_start
                matched_pattern = pattern
                matched_text = match.group(0)
    
    if start_idx == -1:
        # Try a more flexible approach for blocks that might not match standard patterns
        # This is especially useful for resource blocks which have a more complex structure
        flexible_pattern = fr'(?:^|\n)\s*{block_type}\s+(?:"[^"]*"\s+)?(?:"?{escaped_block_name}"?|\'?{escaped_block_name}\'?)'
        match = re.search(flexible_pattern, raw_content, re.MULTILINE)
        if match:
            # Found a match with more flexible pattern
            start_idx = match.start()
            # Find the first '{' or '=' after this match
            pos = start_idx + len(match.group(0))
            while pos < len(raw_content) and raw_content[pos] not in '{=':
                pos += 1
                
            if pos < len(raw_content):
                if raw_content[pos] == '=':
                    matched_pattern = '='  # This is an equals block
                else:
                    matched_pattern = '{'  # This is a braced block
        
        if start_idx == -1:
            return "Block content not found"
    
    # Determine if this is an equals-style block or brace-style block
    is_equals_block = matched_pattern and ('=' in matched_pattern or matched_pattern == '=')
    
    # For equals-style blocks (like variable declarations)
    if is_equals_block:
        # Find the equals sign
        eq_pos = raw_content.find('=', start_idx)
        if eq_pos == -1:
            return "Block content not found (equals sign expected)"
        
        # Look for either the next block definition or EOF
        block_pattern = r'(?:^|\n)\s*(resource|provider|variable|output|locals|module|data)\s+'
        next_match = re.search(block_pattern, raw_content[eq_pos:], re.MULTILINE)
        
        if next_match:
            end_idx = eq_pos + next_match.start()
            # Go back to the last complete line
            last_newline = raw_content.rfind('\n', 0, end_idx)
            if last_newline > start_idx:
                end_idx = last_newline
        else:
            end_idx = len(raw_content)
            
        # Clean up the content to handle EOF case properly
        content = raw_content[start_idx:end_idx].strip()
        return content
    
    # For brace-style blocks
    # Find the opening brace
    open_brace_pos = -1
    for i in range(start_idx, min(start_idx + 200, len(raw_content))):
        if raw_content[i] == '{':
            open_brace_pos = i
            break
    
    if open_brace_pos == -1:
        return "Block content not found (no opening brace)"
        
    # Count brackets to find matching closing brace
    bracket_count = 1
    current_idx = open_brace_pos
    
    # Find the matching closing bracket
    while bracket_count > 0 and current_idx < len(raw_content) - 1:
        current_idx += 1
        if raw_content[current_idx] == '{':
            bracket_count += 1
        elif raw_content[current_idx] == '}':
            bracket_count -= 1
    
    if bracket_count == 0:
        # Found a matching closing bracket
        end_idx = current_idx + 1
        return raw_content[start_idx:end_idx]
    
    return "Block content not found (incomplete block)"

# Function to edit content in an external editor and save changes
def edit_block_content(file_path, block_type, block_name, block_content):
    """
    Edit a block of Terraform code in an external editor.
    This function is designed to properly handle the terminal state transitions.
    """
    # Ensure file_path is absolute
    if not os.path.isabs(file_path):
        file_path = os.path.abspath(file_path)
    
    # Create a temporary file with the block content
    try:
        with tempfile.NamedTemporaryFile(suffix='.tf', mode='w', delete=False) as temp_file:
            temp_file_path = temp_file.name
            temp_file.write(block_content)
            temp_file.flush()  # Make sure all data is written
    except Exception as e:
        return False, f"Failed to create temp file: {str(e)}"

    # Choose an editor - try to use user's preferred editor from environment
    editor = os.environ.get('EDITOR', 'vi')
    
    try:
        # Note: curses.endwin() has already been called by the main function
        # before this function is called, so terminal should be in normal mode
        
        # Print clear instructions for the user
        os.system('clear')  # Clear screen for better UX
        print("\n\n==== EDITING TERRAFORM BLOCK ====")
        print(f"File: {os.path.basename(file_path)}")
        print(f"Block: {block_type}.{block_name}")
        print(f"Editor: {editor}")
        print("Make your changes and save the file. Then exit the editor to return to the TUI.")
        print("==============================\n")
        
        # Run the editor and wait for it to complete
        result = subprocess.run([editor, temp_file_path])
        
        # Read the edited content
        try:
            with open(temp_file_path, 'r') as temp_file:
                edited_content = temp_file.read()
            
            # Read the original file content
            with open(file_path, 'r') as f:
                original_content = f.read()
            
            # Replace the block content in the original file
            if block_content in original_content:
                new_content = original_content.replace(block_content, edited_content)
                
                # Write the updated content back to the file
                with open(file_path, 'w') as f:
                    f.write(new_content)
                
                success = True
                message = "Changes saved successfully"
            else:
                success = False
                message = "Could not locate the original block in the file"
        except Exception as e:
            success = False
            message = f"Error saving changes: {str(e)}"
        
        # Clean up temp file
        try:
            os.unlink(temp_file_path)
        except:
            pass
        
        # Clear the screen again before returning to curses
        os.system('clear')
        
        return success, message
            
    except Exception as e:
        # Make sure to clean up
        try:
            os.unlink(temp_file_path)
        except:
            pass
        return False, f"Error while editing: {str(e)}"

# Function for Terraform HCL syntax highlighting
def apply_syntax_highlighting(code):
    """
    Apply Terraform HCL syntax highlighting directly in memory.
    The function returns the original code - actual coloring is handled
    by the display function in real-time.
    """
    # We'll use direct color application in the display function
    # without embedding color markers in the text
    return code

# Views
def file_view(stdscr, data, selected_index, selected_block_index, file_scroll_offset=0, block_scroll_offset=0, content_scroll_offset=0, wrap_lines=True):
    height, width = stdscr.getmaxyx()
    
    # Define column widths
    col1_width = max(20, width // 4)
    col2_width = max(20, width // 4)
    col3_width = width - col1_width - col2_width - 4
    
    # Get the list of files
    files = list(data.keys())
    # Filter out raw content files
    files = [f for f in files if not f.endswith("_raw")]
    
    # Get the selected file name for dynamic headers
    selected_file_name = ""
    if 0 <= selected_index < len(files):
        selected_file_name = files[selected_index]
    
    # Get selected block name for dynamic headers
    selected_block_name = ""
    if selected_index < len(files) and selected_block_index >= 0:
        selected_file = files[selected_index]
        file_content = data.get(selected_file, {})
        
        blocks = []
        if isinstance(file_content, dict):
            for block_type, block_items in file_content.items():
                if isinstance(block_items, list):
                    for item in block_items:
                        if isinstance(item, dict):
                            for name in item.keys():
                                blocks.append(f"{block_type}.{name}")
                elif isinstance(block_items, dict):
                    for name in block_items.keys():
                        blocks.append(f"{block_type}.{name}")
        
        if 0 <= selected_block_index < len(blocks):
            selected_block_name = blocks[selected_block_index]
    
    # Draw column headers with borders and dynamic content
    header_y = 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    header_y += 1
    
    # First column header is always "Files"
    col1_header = "Files"
    # Second column header shows the selected file
    col2_header = f"Blocks in {selected_file_name}" if selected_file_name else "Blocks in File"
    # Third column header shows both selected file and block
    if selected_file_name and selected_block_name:
        col3_header = f"{selected_file_name}: {selected_block_name}"
    elif selected_file_name:
        col3_header = f"Content from {selected_file_name}"
    else:
        col3_header = "Raw Block Content"
    
    # Truncate headers if they're too long
    if len(col2_header) > col2_width - 4:
        col2_header = col2_header[:col2_width - 7] + "..."
    if len(col3_header) > col3_width - 4:
        col3_header = col3_header[:col3_width - 7] + "..."
    
    stdscr.addstr(header_y, 1, "| " + col1_header.ljust(col1_width-4) + " | " + 
                  col2_header.ljust(col2_width-4) + " | " + 
                  col3_header.ljust(col3_width-4) + " |")
    header_y += 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    
    # Start row for content
    start_row = header_y + 1
    
    # Calculate max visible rows for content
    max_rows = height - start_row - 2
    
    # Display file list (first column) with scrolling
    visible_files = files[file_scroll_offset:file_scroll_offset + max_rows]
    for i, file in enumerate(visible_files):
        row = start_row + i
        is_selected = i + file_scroll_offset == selected_index
        
        if is_selected:
            stdscr.addstr(row, 1, "| " + ("> " + file).ljust(col1_width-4) + " ", curses.A_REVERSE)
        else:
            stdscr.addstr(row, 1, "| " + ("  " + file).ljust(col1_width-4) + " ")
        
        # Draw column separators
        stdscr.addstr(row, col1_width-1, "|")
    
    # Fill remaining rows in first column
    for i in range(len(visible_files), max_rows):
        row = start_row + i
        stdscr.addstr(row, 1, "|" + " "*(col1_width-2) + "|")
    
    # Display scroll indicators for files if needed
    if file_scroll_offset > 0:
        stdscr.addstr(start_row, col1_width-3, "↑")
    if file_scroll_offset + max_rows < len(files):
        stdscr.addstr(start_row + max_rows - 1, col1_width-3, "↓")
    
    # Display blocks of selected file (second column) with scrolling
    blocks = []
    if selected_index < len(files):
        selected_file = files[selected_index]
        file_content = data.get(selected_file, {})
        
        if isinstance(file_content, dict):
            for block_type, block_items in file_content.items():
                if isinstance(block_items, list):
                    for item in block_items:
                        if isinstance(item, dict):
                            for name in item.keys():
                                blocks.append(f"{block_type}.{name}")
                elif isinstance(block_items, dict):
                    for name in block_items.keys():
                        blocks.append(f"{block_type}.{name}")
        
        # Apply block scrolling
        visible_blocks = blocks[block_scroll_offset:block_scroll_offset + max_rows]
        
        for i, block in enumerate(visible_blocks):
            row = start_row + i
            is_selected = i + block_scroll_offset == selected_block_index
            
            # Handle line wrapping in block column
            if wrap_lines and len(block) > col2_width - 4:
                display_block = block[:col2_width-7] + "..."
            else:
                display_block = block
                
            if is_selected:
                stdscr.addstr(row, col1_width, "| " + display_block.ljust(col2_width-4) + " ", curses.A_REVERSE)
            else:
                stdscr.addstr(row, col1_width, "| " + display_block.ljust(col2_width-4) + " ")
            
            # Draw column separators
            stdscr.addstr(row, col1_width+col2_width-1, "|")
        
        # Fill remaining rows in second column
        for i in range(len(visible_blocks), max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
        
        # Display scroll indicators for blocks if needed
        if block_scroll_offset > 0:
            stdscr.addstr(start_row, col1_width+col2_width-3, "↑")
        if block_scroll_offset + max_rows < len(blocks):
            stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width-3, "↓")
        
        # Display raw content of selected block (third column) with scrolling and optional wrapping
        if 0 <= selected_block_index < len(blocks):
            block = blocks[selected_block_index]
            parts = block.split('.', 1)
            if len(parts) == 2:
                block_type, block_name = parts
                
                # Get raw file content
                raw_content = data.get(selected_file + "_raw", "")
                
                # Special handling for resource blocks - both parts can contain quotes
                if block_type == "resource" and '"' in block_name:
                    # The block_name may contain both the resource type and name
                    # For example: resource."aws_s3_bucket"."guide-tfe-es-s3"
                    if '"' in block_name:
                        # Remove quotes and extract the actual block name
                        block_name = block_name.replace('"', '')
                
                # Find and display the block content
                block_content = extract_block_content(raw_content, block_type, block_name)
                
                # Apply syntax highlighting to the block content
                highlighted_content = apply_syntax_highlighting(block_content)
                
                # Display block content with scrolling, handling line breaks and wrapping
                lines = highlighted_content.split('\n')
                wrapped_lines = []
                
                if wrap_lines:
                    # With line wrapping: Process each line to fit within column width
                    for line in lines:
                        # If the line is shorter than the column width, add it as is
                        if len(line) <= col3_width - 6:  # -6 to account for margin and padding
                            wrapped_lines.append(line)
                        else:
                            # Wrap the line by characters (not words, for simplicity and reliability)
                            start = 0
                            while start < len(line):
                                end = min(start + col3_width - 6, len(line))
                                wrapped_lines.append(line[start:end])
                                start = end
                    # Use wrapped lines for display
                    lines = wrapped_lines
                # else: use the original lines without wrapping
                
                # Display the visible portion of the content
                visible_lines = lines[content_scroll_offset:content_scroll_offset + max_rows]
                
                for i, line in enumerate(visible_lines):
                    row = start_row + i
                    
                    # Apply real-time syntax highlighting
                    # First, display the base line with normal attributes
                    try:
                        stdscr.addstr(row, col1_width+col2_width, "| ", curses.A_NORMAL)
                        
                        # Now check and colorize line parts
                        stripped_line = line.strip()
                        display_line = line
                        
                        # Set default attribute
                        attr = curses.A_NORMAL
                        
                        # Apply syntax highlighting based on pattern matching
                        # Comments
                        if stripped_line.startswith('#') and curses.has_colors():
                            attr = curses.color_pair(4)  # Magenta for comments
                        # Keywords at beginning of line
                        elif (any(stripped_line.startswith(k + " ") for k in ['resource', 'variable', 'provider', 'module', 'data', 'output', 'locals', 'terraform'])
                              and curses.has_colors()):
                            attr = curses.color_pair(1)  # Green for keywords
                        # Lines with quotes - likely strings
                        elif ('"' in stripped_line or "'" in stripped_line) and curses.has_colors():
                            # Only highlight if quotes are balanced - otherwise might be a mistake
                            double_quotes = stripped_line.count('"') % 2 == 0
                            single_quotes = stripped_line.count("'") % 2 == 0
                            if double_quotes or single_quotes:
                                attr = curses.color_pair(2)  # Yellow for strings
                        # Special patterns
                        elif "=" in stripped_line and curses.has_colors():
                            attr = curses.color_pair(3)  # Cyan for assignments
                            
                        # Add the line with highlighting
                        stdscr.addstr(display_line.ljust(col3_width-4) + " ", attr)
                        
                    except curses.error:
                        # This can happen if we try to write to the bottom-right corner
                        pass
                
                # Display scroll indicators for content if needed
                if content_scroll_offset > 0:
                    stdscr.addstr(start_row, col1_width+col2_width+col3_width-3, "↑")
                if content_scroll_offset + max_rows < len(lines):
                    stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width+col3_width-3, "↓")
            else:
                stdscr.addstr(start_row, col1_width+col2_width, "| Invalid block format" + " "*(col3_width-20) + " ")
        
        # Fill remaining rows in third column
        empty_start = 0
        if 0 <= selected_block_index < len(blocks):
            block = blocks[selected_block_index]
            parts = block.split('.', 1)
            if len(parts) == 2:
                raw_content = data.get(selected_file + "_raw", "")
                block_type, block_name = parts
                
                # Special handling for resource blocks as above
                if block_type == "resource" and '"' in block_name:
                    block_name = block_name.replace('"', '')
                    
                block_content = extract_block_content(raw_content, block_type, block_name)
                # Calculate the number of lines if wrapping is enabled
                if wrap_lines:
                    lines = []
                    for original_line in block_content.split('\n'):
                        if len(original_line) <= col3_width - 4:
                            lines.append(original_line)
                        else:
                            # Simple word-wrap for estimation
                            words = original_line.split(' ')
                            current_line = ""
                            for word in words:
                                if len(current_line) + len(word) + 1 <= col3_width - 4:
                                    current_line += (word + ' ')
                                else:
                                    lines.append(current_line)
                                    current_line = word + ' '
                            if current_line:
                                lines.append(current_line)
                else:
                    lines = block_content.split('\n')
                
                visible_lines = lines[content_scroll_offset:]
                empty_start = len(visible_lines)
                if empty_start > max_rows:
                    empty_start = max_rows
        
        for i in range(empty_start, max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    else:
        # Fill empty columns if no file is selected
        for i in range(max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    
    # Draw bottom border
    stdscr.addstr(start_row + max_rows, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")

def category_view(stdscr, data, selected_index, selected_block_index, file_scroll_offset=0, block_scroll_offset=0, content_scroll_offset=0, wrap_lines=True):
    """
    Enhanced category view with three columns like file_view.
    First column shows categories, second column shows items in the selected category,
    and third column shows the content of the selected item.
    """
    height, width = stdscr.getmaxyx()
    
    # Define column widths - same as file_view
    col1_width = max(20, width // 4)
    col2_width = max(20, width // 4)
    col3_width = width - col1_width - col2_width - 4
    
    # Define categories
    categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
    
    # Get selected category name for dynamic headers
    selected_category_name = ""
    if 0 <= selected_index < len(categories):
        selected_category_name = categories[selected_index].upper()
    
    # Draw column headers with borders and dynamic content
    header_y = 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    header_y += 1
    
    # First column header is always "Categories"
    col1_header = "Categories"
    # Second column header shows the selected category
    col2_header = f"Items in {selected_category_name}" if selected_category_name else "Items in Category"
    # Third column header
    col3_header = "Content"
    
    # Truncate headers if they're too long
    if len(col2_header) > col2_width - 4:
        col2_header = col2_header[:col2_width - 7] + "..."
    if len(col3_header) > col3_width - 4:
        col3_header = col3_header[:col3_width - 7] + "..."
    
    stdscr.addstr(header_y, 1, "| " + col1_header.ljust(col1_width-4) + " | " + 
                 col2_header.ljust(col2_width-4) + " | " + 
                 col3_header.ljust(col3_width-4) + " |")
    header_y += 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    
    # Start row for content
    start_row = header_y + 1
    
    # Calculate max visible rows for content
    max_rows = height - start_row - 2
    
    # Display category list (first column) with scrolling
    visible_categories = categories[file_scroll_offset:file_scroll_offset + max_rows]
    for i, category in enumerate(visible_categories):
        row = start_row + i
        is_selected = i + file_scroll_offset == selected_index
        
        if is_selected:
            stdscr.addstr(row, 1, "| " + ("> " + category.upper()).ljust(col1_width-4) + " ", curses.A_REVERSE)
        else:
            stdscr.addstr(row, 1, "| " + ("  " + category.upper()).ljust(col1_width-4) + " ")
        
        # Draw column separators
        stdscr.addstr(row, col1_width-1, "|")
    
    # Fill remaining rows in first column
    for i in range(len(visible_categories), max_rows):
        row = start_row + i
        stdscr.addstr(row, 1, "|" + " "*(col1_width-2) + "|")
    
    # Display scroll indicators for categories if needed
    if file_scroll_offset > 0:
        stdscr.addstr(start_row, col1_width-3, "↑")
    if file_scroll_offset + max_rows < len(categories):
        stdscr.addstr(start_row + max_rows - 1, col1_width-3, "↓")
    
    # Display items of selected category (second column) with scrolling
    items = []
    if selected_index < len(categories):
        selected_category = categories[selected_index]
        
        # Find all items of this category
        for file, content in data.items():
            if file.endswith("_raw"):
                continue
            if selected_category in content:
                if isinstance(content[selected_category], dict):
                    for item_name in content[selected_category]:
                        items.append(f"{file}:{item_name}")
                elif isinstance(content[selected_category], list):
                    for item in content[selected_category]:
                        if isinstance(item, dict):
                            for item_name in item:
                                items.append(f"{file}:{item_name}")
                            for item_name in item:
                                items.append(f"{file}:{item_name}")
        
        # Apply block scrolling
        visible_items = items[block_scroll_offset:block_scroll_offset + max_rows]
        
        for i, item in enumerate(visible_items):
            row = start_row + i
            is_selected = i + block_scroll_offset == selected_block_index
            
            # Handle line wrapping in item column
            if wrap_lines and len(item) > col2_width - 4:
                display_item = item[:col2_width-7] + "..."
            else:
                display_item = item
                
            if is_selected:
                stdscr.addstr(row, col1_width, "| " + display_item.ljust(col2_width-4) + " ", curses.A_REVERSE)
            else:
                stdscr.addstr(row, col1_width, "| " + display_item.ljust(col2_width-4) + " ")
            
            # Draw column separators
            stdscr.addstr(row, col1_width+col2_width-1, "|")
        
        # Fill remaining rows in second column
        for i in range(len(visible_items), max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
        
        # Display scroll indicators for items if needed
        if block_scroll_offset > 0:
            stdscr.addstr(start_row, col1_width+col2_width-3, "↑")
        if block_scroll_offset + max_rows < len(items):
            stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width-3, "↓")
        
        # Display content of selected item (third column) with scrolling
        if 0 <= selected_block_index < len(items):
            pass  # TODO: implement logic here
            selected_item = items[selected_block_index]
            file_name, item_name = selected_item.split(":", 1)
            
            # Get raw file content
            raw_content = data.get(file_name + "_raw", "")
            
            # Extract the block content
            block_content = extract_block_content(raw_content, selected_category, item_name)
            
            # Split into lines for display
            lines = block_content.split('\n')
            if wrap_lines:
                wrapped_lines = []
                for line in lines:
                    if len(line) <= col3_width - 6:
                        wrapped_lines.append(line)
                    else:
                        start = 0
                        while start < len(line):
                            end = min(start + col3_width - 6, len(line))
                            wrapped_lines.append(line[start:end])
                            start = end
                lines = wrapped_lines
            
            # Display the visible portion of the content
            visible_lines = lines[content_scroll_offset:content_scroll_offset + max_rows]
            
            for i, line in enumerate(visible_lines):
                row = start_row + i
                
                # Apply real-time syntax highlighting
                try:
                    stdscr.addstr(row, col1_width+col2_width, "| ", curses.A_NORMAL)
                    
                    # Now check and colorize line parts
                    stripped_line = line.strip()
                    display_line = line
                    
                    # Set default attribute
                    attr = curses.A_NORMAL
                    
                    # Apply syntax highlighting based on pattern matching
                    # Comments
                    if stripped_line.startswith('#') and curses.has_colors():
                        attr = curses.color_pair(4)  # Magenta for comments
                    # Keywords at beginning of line
                    elif (any(stripped_line.startswith(k + " ") for k in ['resource', 'variable', 'provider', 'module', 'data', 'output', 'locals', 'terraform'])
                          and curses.has_colors()):
                        attr = curses.color_pair(1)  # Green for keywords
                    # Lines with quotes - likely strings
                    elif ('"' in stripped_line or "'" in stripped_line) and curses.has_colors():
                        # Only highlight if quotes are balanced - otherwise might be a mistake
                        double_quotes = stripped_line.count('"') % 2 == 0
                        single_quotes = stripped_line.count("'") % 2 == 0
                        if double_quotes or single_quotes:
                            attr = curses.color_pair(2)  # Yellow for strings
                    # Special patterns
                    elif "=" in stripped_line and curses.has_colors():
                        attr = curses.color_pair(3)  # Cyan for assignments
                        
                    # Add the line with highlighting
                    stdscr.addstr(display_line.ljust(col3_width-4) + " ", attr)
                    
                except curses.error:
                    # This can happen if we try to write to the bottom-right corner
                    pass
            
            # Display scroll indicators for content if needed
            if content_scroll_offset > 0:
                stdscr.addstr(start_row, col1_width+col2_width+col3_width-3, "↑")
            if content_scroll_offset + max_rows < len(lines):
                stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width+col3_width-3, "↓")
        
        # Fill remaining rows in third column
        empty_start = 0
        if 0 <= selected_block_index < len(items):
            item = items[selected_block_index]
            parts = item.split(':', 1)
            if len(parts) == 2:
                raw_content = data.get(parts[0] + "_raw", "")
                item_name = parts[1]
                
                block_content = extract_block_content(raw_content, selected_category, item_name)
                # Calculate the number of lines if wrapping is enabled
                if wrap_lines:
                    lines = []
                    for original_line in block_content.split('\n'):
                        if len(original_line) <= col3_width - 4:
                            lines.append(original_line)
                        else:
                            # Simple word-wrap for estimation
                            words = original_line.split(' ')
                            current_line = ""
                            for word in words:
                                if len(current_line) + len(word) + 1 <= col3_width - 4:
                                    current_line += (word + ' ')
                                else:
                                    lines.append(current_line)
                                    current_line = word + ' '
                            if current_line:
                                lines.append(current_line)
                else:
                    lines = block_content.split('\n')
                
                visible_lines = lines[content_scroll_offset:]
                empty_start = len(visible_lines)
                if empty_start > max_rows:
                    empty_start = max_rows
        
        for i in range(empty_start, max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    else:
        # Fill empty columns if no file is selected
        for i in range(max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    
    # Draw bottom border
    stdscr.addstr(start_row + max_rows, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")

def module_view(stdscr, data, selected_index, selected_block_index, file_scroll_offset=0, block_scroll_offset=0, content_scroll_offset=0, wrap_lines=True):
    """
    Enhanced module view with three columns like file_view.
    First column shows files with modules, second column shows modules in the selected file,
    and third column shows the content of the selected module.
    """
    height, width = stdscr.getmaxyx()
    
    # Define column widths - same as file_view
    col1_width = max(20, width // 4)
    col2_width = max(20, width // 4)
    col3_width = width - col1_width - col2_width - 4
    
    # Get list of files with modules
    files_with_modules = []
    for file, content in data.items():
        if file.endswith("_raw"):
            continue
        if 'module' in content and content['module']:
            files_with_modules.append(file)
    
    # Get selected file name for dynamic headers
    selected_file_name = ""
    if 0 <= selected_index < len(files_with_modules):
        selected_file_name = files_with_modules[selected_index]
    
    # Draw column headers with borders and dynamic content
    header_y = 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    header_y += 1
    
    # First column header is always "Files with Modules"
    col1_header = "Files with Modules"
    # Second column header shows the selected file
    col2_header = f"Modules in {selected_file_name}" if selected_file_name else "Modules in File"
    # Third column header
    col3_header = "Module Content"
    
    # Truncate headers if they're too long
    if len(col2_header) > col2_width - 4:
        col2_header = col2_header[:col2_width - 7] + "..."
    if len(col3_header) > col3_width - 4:
        col3_header = col3_header[:col3_width - 7] + "..."
    
    stdscr.addstr(header_y, 1, "| " + col1_header.ljust(col1_width-4) + " | " + 
                 col2_header.ljust(col2_width-4) + " | " + 
                 col3_header.ljust(col3_width-4) + " |")
    header_y += 1
    stdscr.addstr(header_y, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")
    
    # Start row for content
    start_row = header_y + 1
    
    # Calculate max visible rows for content
    max_rows = height - start_row - 2
    
    # Display file list (first column) with scrolling
    visible_files = files_with_modules[file_scroll_offset:file_scroll_offset + max_rows]
    for i, file in enumerate(visible_files):
        row = start_row + i
        is_selected = i + file_scroll_offset == selected_index
        
        if is_selected:
            stdscr.addstr(row, 1, "| " + ("> " + file).ljust(col1_width-4) + " ", curses.A_REVERSE)
        else:
            stdscr.addstr(row, 1, "| " + ("  " + file).ljust(col1_width-4) + " ")
        
        # Draw column separators
        stdscr.addstr(row, col1_width-1, "|")
    
    # Fill remaining rows in first column
    for i in range(len(visible_files), max_rows):
        row = start_row + i
        stdscr.addstr(row, 1, "|" + " "*(col1_width-2) + "|")
    
    # Display scroll indicators for files if needed
    if file_scroll_offset > 0:
        stdscr.addstr(start_row, col1_width-3, "↑")
    if file_scroll_offset + max_rows < len(files_with_modules):
        stdscr.addstr(start_row + max_rows - 1, col1_width-3, "↓")
    
    # Display modules of selected file (second column) with scrolling
    modules = []
    if 0 <= selected_index < len(files_with_modules):
        selected_file = files_with_modules[selected_index]
        file_content = data.get(selected_file, {})
        
        if 'module' in file_content:
            if isinstance(file_content['module'], dict):
                modules = list(file_content['module'].keys())
            elif isinstance(file_content['module'], list):
                for item in file_content['module']:
                    if isinstance(item, dict):
                        modules.extend(list(item.keys()))
        
        # Apply block scrolling
        visible_modules = modules[block_scroll_offset:block_scroll_offset + max_rows]
        
        for i, module in enumerate(visible_modules):
            row = start_row + i
            is_selected = i + block_scroll_offset == selected_block_index
            
            # Handle line wrapping in module column
            if wrap_lines and len(module) > col2_width - 4:
                display_module = module[:col2_width-7] + "..."
            else:
                display_module = module
                
            if is_selected:
                stdscr.addstr(row, col1_width, "| " + display_module.ljust(col2_width-4) + " ", curses.A_REVERSE)
            else:
                stdscr.addstr(row, col1_width, "| " + display_module.ljust(col2_width-4) + " ")
            
            # Draw column separators
            stdscr.addstr(row, col1_width+col2_width-1, "|")
        
        # Fill remaining rows in second column
        for i in range(len(visible_modules), max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
        
        # Display scroll indicators for modules if needed
        if block_scroll_offset > 0:
            stdscr.addstr(start_row, col1_width+col2_width-3, "↑")
        if block_scroll_offset + max_rows < len(modules):
            stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width-3, "↓")
        
        # Display content of selected module (third column) with scrolling
        if 0 <= selected_block_index < len(modules):
            selected_module = modules[selected_block_index]
            
            # Get raw file content
            raw_content = data.get(selected_file + "_raw", "")
            
            # Extract the module content
            block_content = extract_block_content(raw_content, "module", selected_module)
            
            # Split into lines for display
            lines = block_content.split('\n')
            if wrap_lines:
                wrapped_lines = []
                for line in lines:
                    if len(line) <= col3_width - 6:
                        wrapped_lines.append(line)
                    else:
                        start = 0
                        while start < len(line):
                            end = min(start + col3_width - 6, len(line))
                            wrapped_lines.append(line[start:end])
                            start = end
                lines = wrapped_lines
            
            # Display the visible portion of the content
            visible_lines = lines[content_scroll_offset:content_scroll_offset + max_rows]
            
            for i, line in enumerate(visible_lines):
                row = start_row + i
                
                # Apply real-time syntax highlighting
                try:
                    stdscr.addstr(row, col1_width+col2_width, "| ", curses.A_NORMAL)
                    
                    # Now check and colorize line parts
                    stripped_line = line.strip()
                    display_line = line
                    
                    # Set default attribute
                    attr = curses.A_NORMAL
                    
                    # Apply syntax highlighting based on pattern matching
                    # Comments
                    if stripped_line.startswith('#') and curses.has_colors():
                        attr = curses.color_pair(4)  # Magenta for comments
                    # Keywords at beginning of line
                    elif (any(stripped_line.startswith(k + " ") for k in ['resource', 'variable', 'provider', 'module', 'data', 'output', 'locals', 'terraform'])
                          and curses.has_colors()):
                        attr = curses.color_pair(1)  # Green for keywords
                    # Lines with quotes - likely strings
                    elif ('"' in stripped_line or "'" in stripped_line) and curses.has_colors():
                        # Only highlight if quotes are balanced - otherwise might be a mistake
                        double_quotes = stripped_line.count('"') % 2 == 0
                        single_quotes = stripped_line.count("'") % 2 == 0
                        if double_quotes or single_quotes:
                            attr = curses.color_pair(2)  # Yellow for strings
                    # Special patterns
                    elif "=" in stripped_line and curses.has_colors():
                        attr = curses.color_pair(3)  # Cyan for assignments
                        
                    # Add the line with highlighting
                    stdscr.addstr(display_line.ljust(col3_width-4) + " ", attr)
                    
                except curses.error:
                    # This can happen if we try to write to the bottom-right corner
                    pass
            
            # Display scroll indicators for content if needed
            if content_scroll_offset > 0:
                stdscr.addstr(start_row, col1_width+col2_width+col3_width-3, "↑")
            if content_scroll_offset + max_rows < len(lines):
                stdscr.addstr(start_row + max_rows - 1, col1_width+col2_width+col3_width-3, "↓")
        
        # Fill remaining rows in third column
        empty_start = 0
        if 0 <= selected_block_index < len(modules):
            empty_start = min(max_rows, len(visible_lines) if 'visible_lines' in locals() else 0)
        
        for i in range(empty_start, max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    else:
        # Fill empty columns if no file is selected
        for i in range(max_rows):
            row = start_row + i
            stdscr.addstr(row, col1_width, "|" + " "*(col2_width-2) + "|")
            stdscr.addstr(row, col1_width+col2_width, "|" + " "*(col3_width-2) + "|")
    
    # Draw bottom border
    stdscr.addstr(start_row + max_rows, 1, "+" + "-"*(col1_width-2) + "+" + "-"*(col2_width-2) + "+" + "-"*(col3_width-2) + "+")

# Function to check if data contains modules
def has_modules(data):
    """Check if the provided Terraform data has any module definitions."""
    if not isinstance(data, dict):
        return False
    if 'module' not in data:
        return False
    # Check if the module key has any content
    return bool(data['module'])  # Will be True if data['module'] is non-empty

# Functions to handle Terraform workspace operations
def get_current_terraform_workspace():
    """Get the current Terraform workspace"""
    try:
        result = subprocess.run(["terraform", "workspace", "show"], capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "default"  # Assume default workspace if command fails
    except FileNotFoundError:
        return "terraform not found"

def list_terraform_workspaces():
    """List all Terraform workspaces"""
    try:
        result = subprocess.run(["terraform", "workspace", "list"], capture_output=True, text=True, check=True)
        # Parse workspaces, removing leading '* ' from current workspace
        workspaces = []
        for line in result.stdout.strip().split("\n"):
            if line.startswith("*"):
                workspaces.append(line[2:].strip())  # Remove '* ' and any extra whitespace
            else:
                workspaces.append(line.strip())
        return workspaces
    except subprocess.CalledProcessError:
        return ["default"]  # Return default workspace if command fails
    except FileNotFoundError:
        return ["terraform not found"]

def create_terraform_workspace(workspace_name):
    """Create a new Terraform workspace"""
    try:
        result = subprocess.run(["terraform", "workspace", "new", workspace_name], 
                                capture_output=True, text=True, check=True)
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    except FileNotFoundError:
        return False, "terraform command not found"

def select_terraform_workspace(workspace_name):
    """Select an existing Terraform workspace"""
    try:
        result = subprocess.run(["terraform", "workspace", "select", workspace_name], 
                                capture_output=True, text=True, check=True)
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()
    except FileNotFoundError:
        return False, "terraform command not found"

def prompt_for_workspace_name(stdscr, prompt_text="Enter workspace name:"):
    """Prompt user for workspace name"""
    # Save current curses state
    curses.def_prog_mode()
    curses.endwin()
    
    # Get workspace name from user
    print(prompt_text)
    workspace_name = input("> ").strip()
    
    # Restore curses state
    stdscr = curses.initscr()
    curses.reset_prog_mode()
    curses.curs_set(0)  # Hide cursor
    stdscr.refresh()
    
    return workspace_name if workspace_name else None

def init_colors():
    """Initialize color pairs for syntax highlighting"""
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        # Define color pairs to use for highlighting
        curses.init_pair(1, curses.COLOR_GREEN, -1)  # Green for keywords
        curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Yellow for strings
        curses.init_pair(3, curses.COLOR_CYAN, -1)   # Cyan for assignments
        curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # Magenta for comments
        # Error messages
        curses.init_pair(5, curses.COLOR_RED, -1)  # Red for errors

def show_help_screen(stdscr):
    """Display help information"""
    height, width = stdscr.getmaxyx()
    help_text = [
        "Terraform TUI Help:",
        "",
        "Navigation:",
        "  ↑/↓/←/→      Navigate between items",
        "  Tab           Switch between columns",
        "  PgUp/PgDn     Scroll content",
        "",
        "Views:",
        "  Shift+1       File view (files, blocks, content)",
        "  Shift+2       Category view (resource types, items, content)",
        "  Shift+3       Module view (files with modules, modules, content)",
        "",
        "Actions:",
        "  e             Edit selected block in external editor",
        "  s             Save selected block to file",
        "  w             Toggle line wrapping",
        "  Shift+R       Reload files",
        "  Shift+W       Workspace management",
        "  /             Search",
        "  q             Quit application",
        "",
        "Press any key to return..."
    ]
    
    # Clear screen and disable nodelay mode to wait for key press
    stdscr.clear()
    stdscr.nodelay(0)
    
    # Calculate starting position to center text
    start_y = max(0, (height - len(help_text)) // 2)
    
    # Display help text
    for i, line in enumerate(help_text):
        if start_y + i < height:
            centered_x = max(0, (width - len(line)) // 2)
            stdscr.addstr(start_y + i, centered_x, line)
    
    # Wait for key press
    stdscr.getch()
    
    # Restore nodelay mode
    stdscr.nodelay(1)

def prompt_for_search(stdscr, data):
    """Prompt user for search criteria and perform search"""
    # Save current curses state
    curses.def_prog_mode()
    curses.endwin()
    
    print("Enter search term:")
    search_term = input("> ").strip()
    
    # Cancel search if empty
    if not search_term:
        # Restore terminal to curses mode
        stdscr = curses.initscr()
        curses.reset_prog_mode()
        curses.curs_set(0)  # Hide cursor
        return None
    
    # Perform search
    results = []
    for file_name, file_content in data.items():
        # Skip raw files
        if file_name.endswith("_raw"):
            continue
        
        # Get raw content for searching
        raw_content = data.get(file_name + "_raw", "")
        if not raw_content:
            continue
        
        # Case-insensitive search in raw file content
        if search_term.lower() in raw_content.lower():
            # Find block type and name for more specific results
            for block_type, block_items in file_content.items():
                if isinstance(block_items, dict):
                    for block_name, _ in block_items.items():
                        # Look in this specific block
                        block_content = extract_block_content(raw_content, block_type, block_name)
                        if search_term.lower() in block_content.lower():
                            results.append((file_name, block_type, block_name))
                elif isinstance(block_items, list):
                    for item in block_items:
                        if isinstance(item, dict):
                            for block_name, _ in item.items():
                                # Look in this specific block
                                block_content = extract_block_content(raw_content, block_type, block_name)
                                if search_term.lower() in block_content.lower():
                                    results.append((file_name, block_type, block_name))
    
    # Restore terminal to curses mode
    stdscr = curses.initscr()
    curses.reset_prog_mode()
    curses.curs_set(0)  # Hide cursor
    
    return results

def save_block_to_file(block_content, suggested_filename="block.tf"):
    """Save block content to a file"""
    try:
        # Prompt for file name
        print(f"Enter filename to save (default: {suggested_filename}):")
        filename = input("> ").strip()
        
        # Use suggested filename if none provided
        if not filename:
            filename = suggested_filename
            
        # Add .tf extension if not present
        if not filename.endswith(".tf"):
            filename = filename + ".tf"
        
        # Write the content to the file
        with open(filename, 'w') as f:
            f.write(block_content)
            
        return True, f"Saved to {filename}"
    except Exception as e:
        return False, f"Error saving file: {str(e)}"

def show_workspace_menu(stdscr):
    """Display workspace management menu"""
    height, width = stdscr.getmaxyx()
    workspace_menu = [
        "1. List workspaces",
        "2. Create new workspace",
        "3. Select workspace",
        "4. Return to main view"
    ]
    
    current_workspace = get_current_terraform_workspace()
    
    # Save current state and switch to normal terminal mode
    curses.def_prog_mode()
    curses.endwin()
    
    # Clear screen and show menu
    os.system('clear' if os.name == 'posix' else 'cls')
    print("Terraform Workspace Management")
    print(f"Current workspace: {current_workspace}\n")
    
    for item in workspace_menu:
        print(item)
    
    print("\nSelect an option (1-4): ", end="", flush=True)
    choice = input().strip()
    
    result_message = ""
    if choice == "1":
        # List workspaces
        workspaces = list_terraform_workspaces()
        print("\nAvailable workspaces:")
        for workspace in workspaces:
            if workspace == current_workspace:
                print(f"* {workspace} (current)")
            else:
                print(f"  {workspace}")
        print("\nPress Enter to continue...", end="", flush=True)
        input()
    elif choice == "2":
        # Create new workspace
        print("\nEnter new workspace name: ", end="", flush=True)
        new_workspace = input().strip()
        if new_workspace:
            success, message = create_terraform_workspace(new_workspace)
            print(f"\n{message}")
            print("\nPress Enter to continue...", end="", flush=True)
            input()
            result_message = f"Created workspace: {new_workspace}" if success else f"Error: {message}"
        else:
            print("\nNo workspace name provided. Operation cancelled.")
            print("\nPress Enter to continue...", end="", flush=True)
            input()
    elif choice == "3":
        # Select workspace
        workspaces = list_terraform_workspaces()
        print("\nAvailable workspaces:")
        for idx, workspace in enumerate(workspaces, 1):
            if workspace == current_workspace:
                print(f"{idx}. {workspace} (current)")
            else:
                print(f"{idx}. {workspace}")
        
        print("\nSelect workspace number or enter workspace name: ", end="", flush=True)
        selection = input().strip()
        
        # Check if selection is a number (index) or name
        selected_workspace = None
        if selection.isdigit():
            idx = int(selection) - 1
            if 0 <= idx < len(workspaces):
                selected_workspace = workspaces[idx]
        else:
            if selection in workspaces:
                selected_workspace = selection
        
        if selected_workspace:
            success, message = select_terraform_workspace(selected_workspace)
            print(f"\n{message}")
            print("\nPress Enter to continue...", end="", flush=True)
            input()
            result_message = f"Switched to workspace: {selected_workspace}" if success else f"Error: {message}"
        else:
            print("\nInvalid workspace selection.")
            print("\nPress Enter to continue...", end="", flush=True)
            input()
    
    # Restore terminal to curses mode
    stdscr = curses.initscr()
    curses.reset_prog_mode()
    curses.curs_set(0)  # Hide cursor
    return result_message

# Main UI loop
def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(100)
    view = 1
    selected_index = 0
    selected_block_index = -1
    
    # Add scrolling offsets
    file_scroll_offset = 0
    block_scroll_offset = 0
    content_scroll_offset = 0
    
    # Track which column is active (0=files, 1=blocks, 2=content)
    active_column = 0
    
    # Toggle for line wrapping - default is True
    wrap_lines = True
    
    # Initialize color pairs for syntax highlighting if terminal supports it
    init_colors()

    terraform_data = parse_terraform_files(".")
    status_message = "Welcome to Terraform TUI"
    is_error_status = False

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        max_rows = height - 6  # Accounting for headers, borders, and status line
        
        # Get current Terraform workspace
        current_workspace = get_current_terraform_workspace()
        
        # Update instructions to include new features
        stdscr.addstr(0, 2, f"Terraform TUI (1/2/3: Views, ↑↓: Navigate, Tab: Columns, e: Edit, /: Search, h: Help, s: Save, W: Workspace, q: Quit)")
        
        # Display current workspace in the header
        workspace_info = f"Workspace: {current_workspace}"
        # Position workspace info on the right side of the header
        workspace_x = max(0, width - len(workspace_info) - 2)
        stdscr.addstr(0, workspace_x, workspace_info)        # Initialize view-specific variables
        if view == 1:
            files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
            # Make sure selected_index is valid
            if selected_index >= len(files):
                selected_index = 0 if files else -1
                
            # Calculate blocks for the selected file
            blocks = []
            if 0 <= selected_index < len(files):
                selected_file = files[selected_index]
                file_content = terraform_data.get(selected_file, {})
                
                if isinstance(file_content, dict):
                    for block_type, block_items in file_content.items():
                        if isinstance(block_items, list):
                            for item in block_items:
                                if isinstance(item, dict):
                                    for name in item.keys():
                                        blocks.append(f"{block_type}.{name}")
                        elif isinstance(block_items, dict):
                            for name in block_items.keys():
                                blocks.append(f"{block_type}.{name}")
            
            # Make sure selected_block_index is valid
            if selected_block_index >= len(blocks):
                selected_block_index = 0 if blocks else -1
            
            file_view(stdscr, terraform_data, selected_index, selected_block_index, 
                     file_scroll_offset, block_scroll_offset, content_scroll_offset, wrap_lines)
        elif view == 2:
            # Category view displays categories
            categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
            
            # Make sure selected_index is valid
            if selected_index >= len(categories):
                selected_index = 0 if categories else -1
                
            category_view(stdscr, terraform_data, selected_index, selected_block_index,
                          file_scroll_offset, block_scroll_offset, content_scroll_offset, wrap_lines)
        elif view == 3:
            # Module view displays files with modules
            files_with_modules = [f for f in terraform_data.keys() 
                                 if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
            
            # Make sure selected_index is valid
            if selected_index >= len(files_with_modules):
                selected_index = 0 if files_with_modules else -1
                
            module_view(stdscr, terraform_data, selected_index, selected_block_index,
                        file_scroll_offset, block_scroll_offset, content_scroll_offset, wrap_lines)
        
        # Show status message at the bottom of the screen
        if is_error_status and curses.has_colors():
            stdscr.addstr(height-1, 0, status_message, curses.A_REVERSE)
        else:
            stdscr.addstr(height-1, 0, status_message)

        stdscr.refresh()
        key = stdscr.getch()

        if key == ord('q'):
            break
        elif key == ord('!'):  # Shift+1
            view = 1
            selected_block_index = -1
            active_column = 0
            file_scroll_offset = 0
            block_scroll_offset = 0
            content_scroll_offset = 0
        elif key == ord('@'):  # Shift+2
            view = 2
            selected_index = 0  # Reset selection when switching views
            selected_block_index = -1
            active_column = 0
            file_scroll_offset = 0
            block_scroll_offset = 0
            content_scroll_offset = 0
        elif key == ord('#'):  # Shift+3
            view = 3
            selected_index = 0  # Reset selection when switching views
            selected_block_index = -1
            active_column = 0
            file_scroll_offset = 0
            block_scroll_offset = 0
            content_scroll_offset = 0
        elif key == ord('R'):  # Shift+R to reload application
            # Reload all Terraform data
            status_message = "Reloading Terraform files..."
            stdscr.addstr(height-1, 0, status_message)
            stdscr.refresh()
            terraform_data = parse_terraform_files(".")
            # Reset view selections
            selected_block_index = -1 if active_column == 0 else selected_block_index
            file_scroll_offset = 0
            block_scroll_offset = 0
            content_scroll_offset = 0
            status_message = "Application reloaded successfully"
            is_error_status = False
        elif key == ord('W'):  # Shift+W for workspace management
            # Show workspace menu and get result
            result = show_workspace_menu(stdscr)
            if result:
                status_message = result
                # Refresh terraform data in case workspace changed
                terraform_data = parse_terraform_files(".")
            else:
                status_message = f"Current workspace: {get_current_terraform_workspace()}"
            is_error_status = False
        elif key == ord('h'):  # Show help screen
            show_help_screen(stdscr)
            # Need to redraw after help screen
            stdscr.clear()
            status_message = "Welcome back from help screen"
            is_error_status = False
        elif key == ord('/'):  # Search functionality
            search_results = prompt_for_search(stdscr, terraform_data)
            if search_results:
                # If we have results and in file view, try to navigate to the first result
                if view == 1 and search_results:
                    first_result = search_results[0]
                    file_name, block_type, block_name = first_result
                    
                    # Find the file index
                    files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                    try:
                        file_index = files.index(file_name)
                        selected_index = file_index
                        
                        # If we have a block type and name, try to find and select it
                        if block_type and block_name:
                            # Get blocks for this file
                            file_content = terraform_data.get(file_name, {})
                            blocks = []
                            if isinstance(file_content, dict):
                                for bt, block_items in file_content.items():
                                    if isinstance(block_items, list):
                                        for item in block_items:
                                            if isinstance(item, dict):
                                                for bn in item.keys():
                                                    blocks.append(f"{bt}.{bn}")
                                    elif isinstance(block_items, dict):
                                        for bn in block_items.keys():
                                            blocks.append(f"{bt}.{bn}")
                            
                            # Look for the matching block
                            target_block = f"{block_type}.{block_name}"
                            if target_block in blocks:
                                selected_block_index = blocks.index(target_block)
                                active_column = 1  # Move to block column
                            
                        # Adjust scroll positions
                        if selected_index >= max_rows:
                            file_scroll_offset = selected_index - (max_rows // 2)
                        if selected_block_index >= max_rows:
                            block_scroll_offset = selected_block_index - (max_rows // 2)
                    except ValueError:
                        pass  # File not found in the list
                
                # Update status message with result count
                status_message = f"Found {len(search_results)} matches"
                is_error_status = False
            else:
                if search_results is None:  # Search was cancelled
                    status_message = "Search cancelled"
                else:  # Empty search results
                    status_message = "No matches found"
                is_error_status = True
        elif key == ord('s'):  # Save current block to file
            # Only allow saving when we have a block selected
            if (view == 1 and active_column >= 1 and 0 <= selected_block_index) or \
               ((view == 2 or view == 3) and 0 <= selected_block_index):
                
                # Get the appropriate content based on view type
                block_content = ""
                suggested_filename = ""
                
                if view == 1:  # File view
                    files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                    if 0 <= selected_index < len(files):
                        selected_file = files[selected_index]
                        file_content = terraform_data.get(selected_file, {})
                        
                        # Get blocks for this file
                        blocks = []
                        if isinstance(file_content, dict):
                            for block_type, block_items in file_content.items():
                                if isinstance(block_items, list):
                                    for item in block_items:
                                        if isinstance(item, dict):
                                            for name in item.keys():
                                                blocks.append(f"{block_type}.{name}")
                                elif isinstance(block_items, dict):
                                    for name in block_items.keys():
                                        blocks.append(f"{block_type}.{name}")
                        
                        if 0 <= selected_block_index < len(blocks):
                            block = blocks[selected_block_index]
                            parts = block.split('.', 1)
                            if len(parts) == 2:
                                block_type, block_name = parts
                                raw_content = terraform_data.get(selected_file + "_raw", "")
                                
                                # Handle quoted resource names
                                if block_type == "resource" and '"' in block_name:
                                    block_name = block_name.replace('"', '')
                                    
                                block_content = extract_block_content(raw_content, block_type, block_name)
                                suggested_filename = f"{block_type}_{block_name}.tf"
                
                elif view == 2:  # Category view
                    categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                    if 0 <= selected_index < len(categories):
                        selected_category = categories[selected_index]
                        
                        # Find all items of this category
                        items = []
                        for file, content in terraform_data.items():
                            if file.endswith("_raw"):
                                continue
                            if selected_category in content:
                                if isinstance(content[selected_category], dict):
                                    for item_name in content[selected_category]:
                                        items.append(f"{file}:{item_name}")
                                elif isinstance(content[selected_category], list):
                                    for item in content[selected_category]:
                                        if isinstance(item, dict):
                                            for item_name in item:
                                                items.append(f"{file}:{item_name}")
                        
                        if 0 <= selected_block_index < len(items):
                            selected_item = items[selected_block_index]
                            file_name, item_name = selected_item.split(":", 1)
                            
                            # Get raw file content
                            raw_content = terraform_data.get(file_name + "_raw", "")
                            
                            # Extract the block content
                            block_content = extract_block_content(raw_content, selected_category, item_name)
                            suggested_filename = f"{selected_category}_{item_name}.tf"
                
                elif view == 3:  # Module view
                    files_with_modules = []
                    for file, content in terraform_data.items():
                        if file.endswith("_raw"):
                            continue
                        if 'module' in content and content['module']:
                            files_with_modules.append(file)
                            
                    if 0 <= selected_index < len(files_with_modules):
                        selected_file = files_with_modules[selected_index]
                        file_content = terraform_data.get(selected_file, {})
                        
                        modules = []
                        if 'module' in file_content:
                            if isinstance(file_content['module'], dict):
                                modules = list(file_content['module'].keys())
                            elif isinstance(file_content['module'], list):
                                for item in file_content['module']:
                                    if isinstance(item, dict):
                                        modules.extend(list(item.keys()))
                        
                        if 0 <= selected_block_index < len(modules):
                            selected_module = modules[selected_block_index]
                            
                            # Get raw file content
                            raw_content = terraform_data.get(selected_file + "_raw", "")
                            
                            # Extract the module content
                            block_content = extract_block_content(raw_content, "module", selected_module)
                            suggested_filename = f"module_{selected_module}.tf"
                
                # If we have content to save, call the save function
                if block_content:
                    # Properly close curses before launching the save dialogue
                    curses.def_prog_mode()  # Save the current state
                    curses.endwin()         # End curses mode temporarily
                    
                    # Call the save function
                    success, message = save_block_to_file(block_content, suggested_filename)
                    
                    # Restore terminal to curses mode
                    stdscr = curses.initscr()  # Re-initialize the screen
                    curses.reset_prog_mode()   # Restore saved state
                    
                    # Re-establish window attributes and settings
                    curses.curs_set(0)         # Hide cursor
                    if curses.has_colors():
                        init_colors()
                    stdscr.keypad(True)
                    stdscr.nodelay(True)       # Non-blocking input
                    
                    status_message = message
                    is_error_status = not success
                else:
                    status_message = "No content available to save"
                    is_error_status = True
            else:
                status_message = "Select a block to save"
                is_error_status = True
        elif key == ord('w'):  # Toggle line wrapping
            wrap_lines = not wrap_lines
            status_message = f"Line wrapping: {'ON' if wrap_lines else 'OFF'}"
            is_error_status = False
        elif key == ord('e'):  # Edit the current block
            # Only allow editing when in file view with a block selected
            if view == 1 and active_column == 2 and 0 <= selected_block_index:
                files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                if 0 <= selected_index < len(files):
                    selected_file = files[selected_index]
                    file_content = terraform_data.get(selected_file, {})
                    
                    # Get the list of blocks for the selected file
                    blocks = []
                    if isinstance(file_content, dict):
                        for block_type, block_items in file_content.items():
                            if isinstance(block_items, list):
                                for item in block_items:
                                    if isinstance(item, dict):
                                        for name in item.keys():
                                            blocks.append(f"{block_type}.{name}")
                            elif isinstance(block_items, dict):
                                for name in block_items.keys():
                                    blocks.append(f"{block_type}.{name}")
                    
                    if 0 <= selected_block_index < len(blocks):
                        block = blocks[selected_block_index]
                        parts = block.split('.', 1)
                        if len(parts) == 2:
                            block_type, block_name = parts
                            
                            # Get raw file content
                            raw_content = terraform_data.get(selected_file + "_raw", "")
                            
                            # Special handling for resource blocks - both parts can contain quotes
                            if block_type == "resource" and '"' in block_name:
                                block_name = block_name.replace('"', '')
                            
                            # Find the block content
                            block_content = extract_block_content(raw_content, block_type, block_name)
                            
                            # Prepare to launch the editor
                            status_message = "Launching editor..."
                            h, w = stdscr.getmaxyx()
                            stdscr.addstr(h-1, 0, status_message)
                            stdscr.refresh()
                            
                            # Properly close curses before launching the editor
                            curses.def_prog_mode()  # Save the current state
                            curses.endwin()         # End curses mode temporarily
                            
                            # Call the editor
                            file_path = os.path.join(".", selected_file)
                            success, message = edit_block_content(file_path, block_type, block_name, block_content)
                            
                            # Restore terminal to curses mode
                            stdscr = curses.initscr()  # Re-initialize the screen
                            curses.reset_prog_mode()   # Restore saved state
                            
                            # Re-establish window attributes and settings
                            curses.curs_set(0)         # Hide cursor
                            if curses.has_colors():
                                init_colors()
                            stdscr.keypad(True)
                            stdscr.nodelay(True)       # Non-blocking input
                            
                            if success:
                                # Reload the terraform data to get updated content
                                terraform_data = parse_terraform_files(".")
                                status_message = message
                                is_error_status = False
                            else:
                                status_message = message
                                is_error_status = True
                        else:
                            status_message = "Invalid block format for editing"
                            is_error_status = True
            elif view != 1:
                status_message = "Editing is only available in file view"
                is_error_status = True
            else:
                status_message = "Select a block to edit (use Tab to move to blocks)"
                is_error_status = True
        elif view == 1:
            # Get list of files (excluding _raw entries)
            files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
            
            # Get blocks for the current file
            blocks = []
            if selected_index < len(files):
                selected_file = files[selected_index]
                file_content = terraform_data.get(selected_file, {})
                
                if isinstance(file_content, dict):
                    for block_type, block_items in file_content.items():
                        if isinstance(block_items, list):
                            for item in block_items:
                                if isinstance(item, dict):
                                    for name in item.keys():
                                        blocks.append(f"{block_type}.{name}")
                        elif isinstance(block_items, dict):
                            for name in block_items.keys():
                                blocks.append(f"{block_type}.{name}")
            
            # Get content lines for wrapping calculation
            content_lines = []
            if 0 <= selected_block_index < len(blocks) and selected_index < len(files):
                selected_file = files[selected_index]
                block = blocks[selected_block_index]
                parts = block.split('.', 1)
                if len(parts) == 2:
                    block_type, block_name = parts
                    raw_content = terraform_data.get(selected_file + "_raw", "")
                    
                    # Handle quoted resource names
                    if block_type == "resource" and '"' in block_name:
                        block_name = block_name.replace('"', '')
                        
                    block_content = extract_block_content(raw_content, block_type, block_name)
                    # If wrapping is enabled, calculate wrapped lines
                    if wrap_lines:
                        col3_width = max(20, (width - 40) // 1)  # Approximate column width
                        tmp_lines = []
                        for original_line in block_content.split('\n'):
                            if len(original_line) <= col3_width - 4:
                                tmp_lines.append(original_line)
                            else:
                                # Simple word-wrap algorithm
                                words = original_line.split(' ')
                                current_line = ""
                                for word in words:
                                    if len(current_line) + len(word) + 1 <= col3_width - 4:
                                        current_line += (word + ' ')
                                    else:
                                        tmp_lines.append(current_line)
                                        current_line = word + ' '
                                if current_line:
                                    tmp_lines.append(current_line)
                        content_lines = tmp_lines
                    else:
                        content_lines = block_content.split('\n')
            
            if key == curses.KEY_DOWN:
                # Handle navigation based on active column
                if active_column == 0:  # File column
                    # Get list of files based on current view
                    if view == 1:
                        files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                    elif view == 2:
                        # For category view, files are categories
                        files = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                    elif view == 3:
                        # For module view
                        files = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                    
                    if selected_index < len(files) - 1:
                        selected_index += 1
                        # Adjust scroll if selection goes out of view
                        if selected_index >= file_scroll_offset + max_rows:
                            file_scroll_offset += 1
                        # Reset block selection when changing files
                        selected_block_index = -1
                        block_scroll_offset = 0
                        content_scroll_offset = 0
                        active_column = 0  # Ensure we stay in file column
                elif active_column == 1:  # Block column
                    # Calculate blocks based on current view and selection
                    blocks = []
                    if view == 1 and 0 <= selected_index < len(files):
                        selected_file = files[selected_index]
                        file_content = terraform_data.get(selected_file, {})
                        
                        if isinstance(file_content, dict):
                            for block_type, block_items in file_content.items():
                                if isinstance(block_items, list):
                                    for item in block_items:
                                        if isinstance(item, dict):
                                            for name in item.keys():
                                                blocks.append(f"{block_type}.{name}")
                                elif isinstance(block_items, dict):
                                    for name in block_items.keys():
                                        blocks.append(f"{block_type}.{name}")
                    elif view == 2:
                        # For category view
                        categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                        blocks = []
                        if 0 <= selected_index < len(categories):
                            selected_category = categories[selected_index]
                            
                            # Find all items of this category
                            for file, content in terraform_data.items():
                                if file.endswith("_raw"):
                                    continue
                                if selected_category in content:
                                    if isinstance(content[selected_category], dict):
                                        for item_name in content[selected_category]:
                                            blocks.append(f"{file}:{item_name}")
                                    elif isinstance(content[selected_category], list):
                                        for item in content[selected_category]:
                                            if isinstance(item, dict):
                                                for item_name in item:
                                                    blocks.append(f"{file}:{item_name}")
                    elif view == 3:
                        # For module view
                        files_with_modules = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                        if 0 <= selected_index < len(files_with_modules):
                            selected_file = files_with_modules[selected_index]
                            file_content = terraform_data.get(selected_file, {})
                            
                            if 'module' in file_content:
                                if isinstance(file_content['module'], dict):
                                    blocks = list(file_content['module'].keys())
                                elif isinstance(file_content['module'], list):
                                    for item in file_content['module']:
                                        if isinstance(item, dict):
                                            blocks.extend(list(item.keys()))
        
                    if selected_block_index < len(blocks) - 1:
                        selected_block_index += 1
                        # Adjust scroll if selection goes out of view
                        if selected_block_index >= block_scroll_offset + max_rows:
                            block_scroll_offset += 1
                        # Reset content scroll when changing blocks
                        content_scroll_offset = 0
                elif active_column == 2:  # Content column (scrolling only)
                    if content_scroll_offset + max_rows < len(content_lines):
                        content_scroll_offset += 1
            
            elif key == curses.KEY_UP:
                # Handle navigation based on active column
                if active_column == 0:  # File column
                    # Get list of files based on current view
                    if view == 1:
                        files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                    elif view == 2:
                        # For category view, files are categories
                        files = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                    elif view == 3:
                        # For module view
                        files = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                    
                    if selected_index > 0:
                        selected_index -= 1
                        # Adjust scroll if selection goes out of view
                        if selected_index < file_scroll_offset:
                            file_scroll_offset -= 1
                elif active_column == 1:  # Block column
                    # Calculate blocks based on current view and selection
                    blocks = []
                    if view == 1 and 0 <= selected_index < len(files):
                        selected_file = files[selected_index]
                        file_content = terraform_data.get(selected_file, {})
                        
                        if isinstance(file_content, dict):
                            for block_type, block_items in file_content.items():
                                if isinstance(block_items, list):
                                    for item in block_items:
                                        if isinstance(item, dict):
                                            for name in item.keys():
                                                blocks.append(f"{block_type}.{name}")
                                elif isinstance(block_items, dict):
                                    for name in block_items.keys():
                                        blocks.append(f"{block_type}.{name}")
                    elif view == 2:
                        # For category view
                        categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                        if 0 <= selected_index < len(categories):
                            selected_category = categories[selected_index]
                            
                            # Find all items of this category
                            for file, content in terraform_data.items():
                                if file.endswith("_raw"):
                                    continue
                                if selected_category in content:
                                    if isinstance(content[selected_category], dict):
                                        for item_name in content[selected_category]:
                                            blocks.append(f"{file}:{item_name}")
                                    elif isinstance(content[selected_category], list):
                                        for item in content[selected_category]:
                                            if isinstance(item, dict):
                                                for item_name in item:
                                                    blocks.append(f"{file}:{item_name}")
                    elif view == 3:
                        # For module view
                        files_with_modules = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                        if 0 <= selected_index < len(files_with_modules):
                            selected_file = files_with_modules[selected_index]
                            file_content = terraform_data.get(selected_file, {})
                            
                            if 'module' in file_content:
                                if isinstance(file_content['module'], dict):
                                    blocks = list(file_content['module'].keys())
                                elif isinstance(file_content['module'], list):
                                    for item in file_content['module']:
                                        if isinstance(item, dict):
                                            blocks.extend(list(item.keys()))
        
                    if selected_block_index > 0:
                        selected_block_index -= 1
                        # Adjust scroll if selection goes out of view
                        if selected_block_index < block_scroll_offset:
                            block_scroll_offset -= 1
                elif active_column == 2:  # Content column (scrolling only)
                    if content_scroll_offset > 0:
                        content_scroll_offset -= 1
            
            elif key == curses.KEY_NPAGE:  # Page Down
                # Handle page down based on active column
                if active_column == 0:  # File column
                    if selected_index + max_rows < len(files):
                        selected_index += max_rows
                        file_scroll_offset += max_rows
                        if file_scroll_offset + max_rows > len(files):
                            file_scroll_offset = max(0, len(files) - max_rows)
                        # Reset block selection when changing files
                        selected_block_index = -1
                        block_scroll_offset = 0
                elif active_column == 1:  # Block column
                    if selected_block_index + max_rows < len(blocks):
                        selected_block_index += max_rows
                        block_scroll_offset += max_rows
                        if block_scroll_offset + max_rows > len(blocks):
                            block_scroll_offset = max(0, len(blocks) - max_rows)
                        # Reset content scroll
                        content_scroll_offset = 0
                elif active_column == 2:  # Content column
                    if content_scroll_offset + max_rows < len(content_lines):
                        content_scroll_offset += max_rows
                        if content_scroll_offset + max_rows > len(content_lines):
                            content_scroll_offset = max(0, len(content_lines) - max_rows)
            
            elif key == curses.KEY_PPAGE:  # Page Up
                # Handle page up based on active column
                if active_column == 0:  # File column
                    if selected_index > 0:
                        selected_index = max(0, selected_index - max_rows)
                        file_scroll_offset = max(0, file_scroll_offset - max_rows)
                        # Reset block selection when changing files
                        selected_block_index = -1
                        block_scroll_offset = 0
                elif active_column == 1:  # Block column
                    if selected_block_index > 0:
                        selected_block_index = max(0, selected_block_index - max_rows)
                        block_scroll_offset = max(0, block_scroll_offset - max_rows)
                        # Reset content scroll
                        content_scroll_offset = 0
                elif active_column == 2:  # Content column
                    if content_scroll_offset > 0:
                        content_scroll_offset = max(0, content_scroll_offset - max_rows)
            
            elif key == 9:  # Tab key to switch columns
                # Get appropriate files list based on the current view
                if view == 1:
                    files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                elif view == 2:
                    # For category view, files are categories
                    files = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                elif view == 3:
                    files = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                
                # Calculate blocks based on view and current selection
                blocks = []
                if view == 1 and 0 <= selected_index < len(files):
                    selected_file = files[selected_index]
                    file_content = terraform_data.get(selected_file, {})
                    
                    if isinstance(file_content, dict):
                        for block_type, block_items in file_content.items():
                            if isinstance(block_items, list):
                                for item in block_items:
                                    if isinstance(item, dict):
                                        for name in item.keys():
                                            blocks.append(f"{block_type}.{name}")
                            elif isinstance(block_items, dict):
                                for name in block_items.keys():
                                    blocks.append(f"{block_type}.{name}")
                elif view == 2:
                    # For category view
                    categories = ["resource", "variable", "data", "output", "locals", "provider", "module", "terraform"]
                    if 0 <= selected_index < len(categories):
                        selected_category = categories[selected_index]
                        
                        # Find all items of this category
                        for file, content in terraform_data.items():
                            if file.endswith("_raw"):
                                continue
                            if selected_category in content:
                                if isinstance(content[selected_category], dict):
                                    for item_name in content[selected_category]:
                                        blocks.append(f"{file}:{item_name}")
                                elif isinstance(content[selected_category], list):
                                    for item in content[selected_category]:
                                        if isinstance(item, dict):
                                            for item_name in item:
                                                blocks.append(f"{file}:{item_name}")
                elif view == 3:
                    # For module view
                    files_with_modules = [f for f in terraform_data.keys() if not f.endswith("_raw") and has_modules(terraform_data.get(f, {}))]
                    if 0 <= selected_index < len(files_with_modules):
                        selected_file = files_with_modules[selected_index]
                        file_content = terraform_data.get(selected_file, {})
                        
                        if 'module' in file_content:
                            if isinstance(file_content['module'], dict):
                                blocks = list(file_content['module'].keys())
                            elif isinstance(file_content['module'], list):
                                for item in file_content['module']:
                                    if isinstance(item, dict):
                                        blocks.extend(list(item.keys()))
        
                if active_column == 0:  # File column is active
                    # Switch to block column if there are blocks
                    if len(blocks) > 0:
                        active_column = 1
                        if selected_block_index == -1:
                            selected_block_index = 0
                elif active_column == 1:  # Block column is active
                    # Get content lines for the selected block
                    content_lines = []
                    if 0 <= selected_block_index < len(blocks) and 0 <= selected_index < len(files):
                        # Logic to get content lines depends on the view
                        if view == 1:
                            selected_file = files[selected_index]
                            block = blocks[selected_block_index]
                            parts = block.split('.', 1)
                            if len(parts) == 2:
                                block_type, block_name = parts
                                raw_content = terraform_data.get(selected_file + "_raw", "")
                                if block_type == "resource" and '"' in block_name:
                                    block_name = block_name.replace('"', '')
                                block_content = extract_block_content(raw_content, block_type, block_name)
                                content_lines = block_content.split('\n')
                        elif view == 2:
                            # For category view, content is the item details
                            selected_category = categories[selected_index]
                            item_name = blocks[selected_block_index].split(":", 1)[-1]
                            
                            # Find the file that contains this item
                            for file, content in terraform_data.items():
                                if file.endswith("_raw"):
                                    continue
                                if selected_category in content:
                                    if isinstance(content[selected_category], dict):
                                        if item_name in content[selected_category]:
                                            # Found the item, extract its content
                                            raw_content = terraform_data.get(file + "_raw", "")
                                            block_content = extract_block_content(raw_content, selected_category, item_name)
                                            content_lines = block_content.split('\n')
                                            break
                                    elif isinstance(content[selected_category], list):
                                        for item in content[selected_category]:
                                            if isinstance(item, dict) and item_name in item:
                                                # Found the item in a list, extract its content
                                                raw_content = terraform_data.get(file + "_raw", "")
                                                block_content = extract_block_content(raw_content, selected_category, item_name)
                                                content_lines = block_content.split('\n')
                                                break
                        elif view == 3:
                            # For module view, content is the module details
                            selected_file = files_with_modules[selected_index]
                            module_name = blocks[selected_block_index]
                            
                            # Get raw content
                            raw_content = terraform_data.get(selected_file + "_raw", "")
                            
                            # Extract module content
                            block_content = extract_block_content(raw_content, "module", module_name)
                            content_lines = block_content.split('\n')
                    
                    # Switch to content column if there's content
                    if 0 <= selected_block_index < len(blocks) and len(content_lines) > 0:
                        active_column = 2
                    else:
                        # Go back to file column if no content
                        active_column = 0
                        selected_block_index = -1
                else:  # Content column is active
                    # Switch back to file column
                    active_column = 0
                    selected_block_index = -1
        elif key == ord('/'):  # Search action
            # Only allow search when in file view
            if view == 1:
                # Prompt for search term
                search_results = prompt_for_search(stdscr, terraform_data)
                
                if search_results:
                    # Display search results in a new view
                    view = 1  # Switch to file view for displaying results
                    selected_index = 0
                    selected_block_index = -1
                    
                    # Prepare search result data structure
                    search_data = {}
                    for file_name, block_type, block_name in search_results:
                        if file_name not in search_data:
                            search_data[file_name] = {}
                        if block_type:
                            if block_type not in search_data[file_name]:
                                search_data[file_name][block_type] = {}
                            if block_name:
                                if block_name not in search_data[file_name][block_type]:
                                    search_data[file_name][block_type][block_name] = {}
                    
                    terraform_data = search_data  # Replace current data with search results
                    file_scroll_offset = 0
                    block_scroll_offset = 0
                    content_scroll_offset = 0
                else:
                    status_message = "No results found"
                    is_error_status = True
            else:
                status_message = "Search is only available in file view"
                is_error_status = True
        elif key == ord('s'):  # Save action
            # Only allow save when in file view with a block selected
            if view == 1 and active_column == 2 and 0 <= selected_block_index:
                files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                if 0 <= selected_index < len(files):
                    selected_file = files[selected_index]
                    file_content = terraform_data.get(selected_file, {})
                    
                    # Get the list of blocks for the selected file
                    blocks = []
                    if isinstance(file_content, dict):
                        for block_type, block_items in file_content.items():
                            if isinstance(block_items, list):
                                for item in block_items:
                                    if isinstance(item, dict):
                                        for name in item.keys():
                                            blocks.append(f"{block_type}.{name}")
                            elif isinstance(block_items, dict):
                                for name in block_items.keys():
                                    blocks.append(f"{block_type}.{name}")
                    
                    if 0 <= selected_block_index < len(blocks):
                        block = blocks[selected_block_index]
                        parts = block.split('.', 1)
                        if len(parts) == 2:
                            block_type, block_name = parts
                            
                            # Get raw file content
                            raw_content = terraform_data.get(selected_file + "_raw", "")
                            
                            # Special handling for resource blocks - both parts can contain quotes
                            if block_type == "resource" and '"' in block_name:
                                block_name = block_name.replace('"', '')
                            
                            # Find the block content
                            block_content = extract_block_content(raw_content, block_type, block_name)
                            
                            # Prompt for filename to save
                            status_message = "Saving block to file..."
                            h, w = stdscr.getmaxyx()
                            stdscr.addstr(h-1, 0, status_message)
                            stdscr.refresh()
                            
                            success, message = save_block_to_file(block_content, suggested_filename=block_name)
                            if success:
                                status_message = message
                                is_error_status = False
                            else:
                                status_message = message
                                is_error_status = True
                        else:
                            status_message = "Invalid block format for saving"
                            is_error_status = True
        elif key == ord('h'):  # Help action
            show_help_screen(stdscr)
        elif key == ord('r'):  # Reload action
            # Reload the current view
            if view == 1:
                # Reload file view data
                files = [f for f in terraform_data.keys() if not f.endswith("_raw")]
                if 0 <= selected_index < len(files):
                    selected_file = files[selected_index]
                    terraform_data = parse_terraform_files(".")
                    # Reset scroll offsets
                    file_scroll_offset = 0
                    block_scroll_offset = 0
                    content_scroll_offset = 0
            else:
                # For other views, just reload the data
                terraform_data = parse_terraform_files(".")
        
        # Additional key bindings for testing
        elif key == ord('t'):  # Test action (toggle view for testing)
            if view == 1:
                view = 2
            elif view == 2:
                view = 3
            else:
                view = 1

# Move the main wrapper call to the end of the file
if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully
        pass
