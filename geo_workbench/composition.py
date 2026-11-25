"""Shared visible-word accounting and page-plan feasibility checks."""
from __future__ import annotations
import re

FAQ_HEADING='Frequently asked questions'
BLOG_LINK='Explore the product details'
CONTRACT_VERSION='1.0'
WORD_PATTERN=re.compile(r"\b[\w]+(?:['’-][\w]+)*\b",re.UNICODE)

def count_words(text):
    return len(WORD_PATTERN.findall(text))

def page_words(page):
    pieces=[page['h1'],FAQ_HEADING]+[s['heading'] for s in page['sections']]+[b['text'] for s in page['sections'] for b in s['blocks']]+[v for f in page['faqs'] for v in (f['question'],f['answer'])]
    if page['page_type']=='blog': pieces.append(BLOG_LINK)
    return count_words(' '.join(pieces))

def execution_diagnostics(plan,ir):
    """Compare uniquely matched planned and written units; do not guess mappings."""
    def norm(value): return ' '.join(value.split()).casefold() if isinstance(value,str) else ''
    pages={}
    for page in ir['pages']:
        kind=page['page_type'];outline=plan.get('blog_outline' if kind=='blog' else 'product_outline') or {}
        result={'matched':[],'unmatched':[]};pages[kind]=result
        for group,planned,written,key in (
            ('section',outline.get('sections',[]),page['sections'],'h2'),
            ('faq',outline.get('faq_ideas',[]),page['faqs'],'question')):
            if not isinstance(planned,list): planned=[]
            index={}
            for row in planned:
                if isinstance(row,dict) and norm(row.get(key)): index.setdefault(norm(row[key]),[]).append(row)
            for unit in written:
                title=unit['heading'] if group=='section' else unit['question'];matches=index.get(norm(title),[])
                budget=matches[0].get('visible_word_range') if len(matches)==1 else None
                valid=isinstance(budget,list) and len(budget)==2 and all(isinstance(x,int) and not isinstance(x,bool) and x>0 for x in budget) and budget[0]<=budget[1]
                if not valid:
                    result['unmatched'].append({'kind':group,'id':unit['id'],'reason':'没有唯一可用的规划范围'});continue
                text=(unit['heading']+' '+' '.join(x['text'] for x in unit['blocks'])) if group=='section' else unit['question']+' '+unit['answer']
                actual=count_words(text);status='under' if actual<budget[0] else ('over' if actual>budget[1] else 'within')
                result['matched'].append({'kind':group,'id':unit['id'],'heading':title,'actual':actual,'planned_range':budget,'status':status,
                                          'information_task':matches[0].get('purpose') if group=='section' else matches[0].get('answer_direction'),
                                          'expression_strategy':matches[0].get('expression_strategy')})
    return {'pages':pages,'notice':'仅比较能唯一匹配的内容；分段范围是弹性写作线索，最终以整页字数及内容质量检查为准。'}

def plan_warnings(plan,contract):
    """Check interval feasibility; this does not judge writing quality or facts.

    Section ranges include heading + body. FAQ ranges include question + answer.
    H1, the fixed FAQ heading and the blog's fixed link are counted separately.
    """
    if plan.get('composition_contract_version')!=CONTRACT_VERSION: return []
    warnings=[]
    def warn(page,field,reason): warnings.append({'page':page,'field':field,'reason':reason})
    if not isinstance(contract,dict) or not isinstance(contract.get('content_requests'),dict):
        return [{'page':'both','field':'composition','reason':'缺少字数目标，无法检查篇幅规划'}]
    for page,outline_name in (('blog','blog_outline'),('product_page','product_outline')):
        outline=plan.get(outline_name);request=contract['content_requests'].get(page,{})
        bounds=request.get('target_words') if isinstance(request,dict) else None
        if not isinstance(outline,dict) or not isinstance(bounds,list) or len(bounds)!=2:
            warn(outline_name,'composition','大纲或目标字数缺失');continue
        sections=outline.get('sections');faqs=outline.get('faq_ideas');h1=outline.get('h1')
        if not isinstance(h1,str) or not h1.strip() or not isinstance(sections,list) or not sections or not isinstance(faqs,list) or len(faqs)<3:
            warn(outline_name,'composition','缺少标题、章节或至少三组 FAQ');continue
        fixed=count_words(h1)+count_words(FAQ_HEADING)+(count_words(BLOG_LINK) if page=='blog' else 0)
        ranges=[]
        for group,rows in (('sections',sections),('faq_ideas',faqs)):
            for i,row in enumerate(rows):
                field=f'{group}[{i}]'
                if not isinstance(row,dict): warn(outline_name,field,'规划单元不是对象');continue
                heading=row.get('h2') if group=='sections' else row.get('question')
                if not isinstance(heading,str) or not heading.strip(): warn(outline_name,field,'缺少章节标题或 FAQ 问题')
                if group=='sections' and (not isinstance(row.get('purpose'),str) or not row['purpose'].strip()): warn(outline_name,field,'缺少章节的信息任务')
                if not isinstance(row.get('expression_strategy'),str) or not row['expression_strategy'].strip(): warn(outline_name,field,'缺少该部分的表达方式与信息安排')
                if group=='faq_ideas' and (not isinstance(row.get('answer_direction'),str) or not row['answer_direction'].strip()): warn(outline_name,field,'缺少 FAQ 的回答方向')
                budget=row.get('visible_word_range')
                if not isinstance(budget,list) or len(budget)!=2 or any(not isinstance(n,int) or isinstance(n,bool) or n<=0 for n in budget) or budget[0]>budget[1]:
                    warn(outline_name,field,'可见字数范围须为正整数 [下限, 上限]');continue
                heading_words=count_words(heading) if isinstance(heading,str) else 0
                if budget[1]<=heading_words:
                    warn(outline_name,field,'范围上限没有给正文或答案留下空间');continue
                # A loose lower bound may be below the heading itself; the
                # feasible part still starts after the heading plus one word.
                ranges.append([max(budget[0],heading_words+1),budget[1]])
        if len(ranges)!=len(sections)+len(faqs): continue
        low=fixed+sum(b[0] for b in ranges);high=fixed+sum(b[1] for b in ranges)
        if low>bounds[1] or high<bounds[0]:
            warn(outline_name,'composition_budget',f'各部分加固定文字的可见字数范围 {low}–{high} 与目标 {bounds[0]}–{bounds[1]} 不相交；应调整信息安排')
    return warnings
