"""Calendar intervals for explicit event queries; unknown dates stay unknown."""
import calendar
import re
from datetime import date, timedelta

def bounds(value):
    parts = [int(p) for p in value.split('-')]
    year = parts[0];month = parts[1] if len(parts)>1 else 1
    first = date(year,month,parts[2] if len(parts)>2 else 1)
    last = first if len(parts)==3 else date(year,month,calendar.monthrange(year,month)[1]) if len(parts)==2 else date(year,12,31)
    return first,last

def windows(query,today=None):
    today=today or date.today()
    normalized=re.sub(r'(\d{4})年(\d{1,2})月(?:(\d{1,2})[日号])?',lambda m:f'{m[1]}-{int(m[2]):02d}'+(f'-{int(m[3]):02d}' if m[3] else ''),query)
    matches=list(re.finditer(r'(?<!\d)\d{4}(?:-\d{2})?(?:-\d{2})?(?!\d)',normalized))
    explicit=[]
    for m in matches:
        try:explicit.append(bounds(m[0]))
        except ValueError:return [] # Invalid dates never broaden into unrestricted recall.
    if explicit:
        if len(explicit)==2 and re.search(r'到|至|~|—|\bto\b|\bthrough\b',normalized[matches[0].end():matches[1].start()],re.I):
            return [(explicit[0][0],explicit[1][1])] if explicit[0][0]<=explicit[1][1] else []
        return explicit
    if re.search(r'昨天|\byesterday\b',query,re.I):return [(today-timedelta(days=1),today-timedelta(days=1))]
    if re.search(r'今天|\btoday\b',query,re.I):return [(today,today)]
    if re.search(r'上个月|上月|\blast month\b',query,re.I):
        last=today.replace(day=1)-timedelta(days=1);return [(last.replace(day=1),last)]
    if re.search(r'本月|这个月|\bthis month\b',query,re.I):return [(today.replace(day=1),today)]
    if re.search(r'去年|\blast year\b',query,re.I):return [(date(today.year-1,1,1),date(today.year-1,12,31))]
    if re.search(r'今年|\bthis year\b',query,re.I):return [(date(today.year,1,1),today)]
    if re.search(r'上周|\blast week\b',query,re.I):
        last=today-timedelta(days=today.weekday()+1);return [(last-timedelta(days=6),last)]
    if re.search(r'本周|这周|\bthis week\b',query,re.I):return [(today-timedelta(days=today.weekday()),today)]
    match=re.search(r'(?:最近|过去)(\d{1,3})天|\blast (\d{1,3}) days\b',query,re.I)
    if match:
        days=int(match[1] or match[2]);return [(today-timedelta(days=days-1),today)] if days else []
    return None

def matches(row,periods):
    if periods is None:return True
    if not row.get('occurred'):return False
    lo=bounds(row['occurred'])[0];hi=bounds(row.get('ended') or row['occurred'])[1]
    return any(lo<=end and hi>=start for start,end in periods)
