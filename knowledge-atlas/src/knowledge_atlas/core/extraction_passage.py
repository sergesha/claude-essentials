"""Select Wiki prose and evidence tables for native statement extraction."""
import re

_NAVIGATION_HEADINGS = {'map', 'wiki map', 'navigation', 'contents', 'table of contents',
                        'related', 'related pages', 'see also', 'page directory'}
_DIRECTORY_COLUMNS = {'page', 'wiki page', 'link', 'title', 'type', 'kind', 'topic', 'category'}
_LINK = r'(?:\[[^\]\n]+\]\([^()\n]+\)|\[\[[^\]\n]+\]\])'


def _cells(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def _directory_table(lines, index):
    header = [cell.casefold() for cell in _cells(lines[index])]
    if (header[0] not in {'page', 'wiki page', 'link', 'title'} or not set(header) <= _DIRECTORY_COLUMNS
            or index + 2 >= len(lines) or not lines[index + 1].lstrip().startswith('|')):
        return index
    divider = _cells(lines[index + 1])
    if len(divider) != len(header) or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in divider):
        return index
    end = index + 2
    while end < len(lines) and lines[end].lstrip().startswith('|'):
        cells = _cells(lines[end])
        if len(cells) != len(header) or not re.fullmatch(_LINK, cells[0]):
            return index
        end += 1
    return end if end > index + 2 else index


def extraction_passage(markdown):
    """Omit link-only navigation in named sections. Keep original evidence lines."""
    lines = markdown.splitlines(keepends=True)
    result = []
    index = 0
    navigation = False
    fence = None
    while index < len(lines):
        line = lines[index]
        index += 1
        delimiter = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line.rstrip('\r\n'))
        if delimiter:
            marker, tail = delimiter.groups()
            if fence is None:
                fence = marker
            elif marker.startswith(fence) and not tail.strip():
                fence = None
            result.append(line)
            continue
        if not fence:
            heading = re.match(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', line)
            if heading:
                navigation = heading[1].casefold() in _NAVIGATION_HEADINGS
            elif navigation and re.fullmatch(r' {0,3}(?:[-*+]\s+)?' + _LINK + r'\s*', line):
                continue
            elif navigation and re.match(r'^ {0,3}\|', line):
                end = _directory_table(lines, index - 1)
                if end > index:
                    index = end
                    continue
        result.append(line)
    return ''.join(result)
