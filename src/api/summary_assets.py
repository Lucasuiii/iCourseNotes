"""Resolve only validated, lesson-scoped classroom JPEGs for portable exports."""
import base64
import io
import json
import zipfile

from src.pipeline.summary_figures import FIGURE_REF, PREFIX, validate_assets


def load_figures(db, lecture):
    row = db.conn.execute('SELECT value FROM meta WHERE key=?',
                          (PREFIX+str(lecture['sub_id']),)).fetchone()
    lecture['figure_state'] = json.loads(row[0]) if row else None
    lecture_assets(lecture)
    return lecture


def lecture_assets(lecture):
    refs = {m[1] for m in FIGURE_REF.findall(lecture['summary'])}
    state = lecture.get('figure_state')
    if state is None:
        if refs:
            raise ValueError('Classroom figure assets are missing')
        return {}
    sub_id = str(lecture['sub_id'])
    if str(state.get('course_id')) != str(lecture['course_id']):
        raise ValueError('Classroom figures belong to another course')
    validate_assets(state, sub_id)
    assets = {f['id']: base64.b64decode(f['data'], validate=True) for f in state['figures']}
    if refs != set(assets):
        raise ValueError('Classroom figure references do not match assets')
    return assets


def portable_attachment(markdown_text, filename, lectures):
    """Return Markdown alone, or a ZIP containing Markdown and its JPEGs."""
    assets = {}
    for lecture in lectures:
        assets.update(lecture_assets(lecture))
    if not assets:
        return markdown_text.encode('utf-8'), filename
    markdown_text = FIGURE_REF.sub(lambda m: '!['+m[1]+'](figures/'+m[2]+'.jpg)', markdown_text)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as package:
        package.writestr(filename, markdown_text.encode('utf-8'))
        for digest, raw in assets.items():
            package.writestr('figures/'+digest+'.jpg', raw)
    return output.getvalue(), filename.removesuffix('.md')+'.zip'


def image_subtype(raw):
    return 'jpeg' if raw.startswith(b'\xff\xd8\xff') else 'png'
