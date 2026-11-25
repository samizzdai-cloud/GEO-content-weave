"""Render an editable SVG map; optional PNG preview uses the same layout data."""
import argparse,html,math,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
W,H=1680,1450
# title, purpose and loop boundaries live in editable text, not a raster image.
NODES=[
 ('input',50,150,460,90,['输入规范化 · 程序','独立商品 / SKU 范围 / 来源与版本'],'program'),
 ('intake',50,285,460,110,['入口审核 / 对话 Agent','发现冲突、读取事实、预览候选差异','事实修改只在用户确认后生效'],'agent'),
 ('input_gate',50,435,460,70,['用户处理输入问题','候选确认后重新审核输入'],'human'),
 ('audience',50,555,220,165,['画像 / 访谈角色','探索目标与购买问题','采访者 ↔ 模拟使用者','每画像两轮问答','各自历史隔离'],'agent'),
 ('research',290,555,220,165,['联网研究角色','品类 / 竞品来源','搜索 ≤2 / 读取 ≤3','摘录程序定位','不补成商品性能'],'agent'),
 ('synthesis',50,775,460,95,['问题地图综合 Agent','汇总决策路径与问题，不冒充真实频率'],'agent'),
 ('planner',50,925,460,115,['Planner','组织编辑主线、两页大纲、FAQ 与 Brief','区分商品事实、品类证据与未知项'],'agent'),
 ('plan_gate',50,1095,460,90,['用户确认当前规划','显式启动内容生产'],'human'),
 ('approved',610,150,460,90,['冻结已批准的输入、规划与证据','版本摘要与审批校验 · 程序'],'program'),
 ('writer',610,285,460,110,['Writer + word_count 工具','组织两页 ContentIR，检查字数与引用','候选修改循环，最多 4 次模型调用'],'agent'),
 ('checker',610,450,460,115,['事实核验 Agent','逐单元判断支持 / 冲突 / 未知','程序拥有原文、单元 ID 与版本'],'agent'),
 ('editor',610,620,460,115,['Editor + 局部编辑工具','按当前旧值与基线修改，保留引用','首次 ≤4；返工最多剩余 3；共享 ≤7'],'agent'),
 ('regression',610,790,460,110,['事实回归 Agent','重核编辑后版本；必要时一次返工阶段','最终仍不通过 → 保留草稿待处理'],'agent'),
 ('render',610,1010,460,115,['渲染与结构检查 · 程序','同一 IR → HTML / Markdown / JSON-LD','事实及结构通过后文字可独立交付'],'program'),
 ('image_input',1170,150,460,90,['已完成正文 + 事实 + 目标场景 + 风格','Blog 双候选；详情图单候选'],'program'),
 ('image_agent',1170,285,460,130,['图片 Agent · 同一定义 / 独立会话','构思 → 内部自查 → 修正 → 最终 Prompt','按商品判断实际使用者，不固定人类 / 性别','低思考；不输出营销分析或内部推理'],'agent'),
 ('seedream',1170,480,460,90,['Seedream · 生图工具','尺寸 / 背景 / 参考原图 / PNG'],'program'),
 ('vision',1170,635,460,110,['程序文件检查 + 同 Agent 实际读图','检查比例 / alpha、主体、场景及请求','模型 pass 不等于实物或营销效果证明'],'agent'),
 ('preview',1170,965,460,115,['对应 Tab：双候选 / 历史 / 修改框','单卡改图只传本卡一张原图','人工修改：生成 1 张后返回，不后置自检'],'human'),
 ('release',1170,1140,460,90,['用户选择并确认当前图文','正文 + 确切图片版本 → PageRelease'],'human'),
 ('output',1170,1285,460,85,['最终交付 · 程序','单页嵌图 HTML / 两页离线 ZIP / 记录'],'program'),
]
EDGES=[
 ([(280,240),(280,285)],''), ([(280,395),(280,435)],''),
 ([(50,470),(35,470),(35,340),(50,340)],''),
 ([(280,505),(280,530),(160,530),(160,555)],''), ([(280,530),(400,530),(400,555)],''),
 ([(160,720),(160,750),(280,750),(280,775)],''), ([(400,720),(400,750),(280,750)],''),
 ([(280,870),(280,925)],''), ([(280,1040),(280,1095)],''),
 ([(510,1140),(545,1140),(545,530),(280,530)],''),
 ([(840,240),(840,285)],''), ([(840,395),(840,450)],''), ([(840,565),(840,620)],''),
 ([(610,375),(597,375),(597,315),(610,315)],''),
 ([(840,735),(840,790)],''), ([(840,900),(840,1010)],'通过'),
 ([(1070,845),(1110,845),(1110,677),(1070,677)],'返工'),
 ([(1400,240),(1400,285)],''), ([(1400,415),(1400,480)],''), ([(1400,570),(1400,635)],''),
 ([(1400,745),(1400,765),(1610,765),(1610,940),(1400,940),(1400,965)],'通过 / 达上限 / 未知'),
 ([(1630,690),(1650,690),(1650,365),(1630,365)],''),
 ([(1170,1020),(1140,1020),(1140,350),(1170,350)],''),
 ([(1400,1080),(1400,1140)],''), ([(1400,1230),(1400,1285)],''),
]
COLORS={'agent':('#eef4ff','#3b66ac'),'program':('#f1f5f9','#65758b'),'human':('#fff6e5','#bb7c19')}
LABELS=[(38,44,'Agent 链路、目的与循环',32),(38,82,'按商品独立任务 · 模型负责理解表达 · 程序负责来源、版本、预算与交付',20),
 (70,128,'01  研究与规划',25),(630,128,'02  写作、核验与编辑',25),(1190,128,'03  图片与图文交付',25),
 (65,1215,'退回规划 → 重研（当前不是定向重跑）',18),
 (65,1255,'研究两分支并行 → 汇合规划',21),(65,1292,'用户等待不占执行槽',20),
 (620,1190,'文字尝试：总调用 ≤14 / 480 秒',21),(620,1225,'预算不足或未知 → 停止并保留现有产物',20),
 (1185,810,'自动返工：每候选生成 ≤3 次',21),(1185,845,'Blog 两候选合计 ≤6；未知不自动重画',19),
 (1185,900,'人工改图 → 同 Agent（左侧回线）',19),
 (38,1410,'蓝色：模型角色 / 工具循环    灰色：确定性程序 / 外部工具    黄色：用户操作    角色复用模型，不等于独立进程',19)]


def render(preview=False):
    labels=list(LABELS)
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc">',
         '<title id="title">GEO-AIGC Agent workflow and loops</title>',
         '<desc id="desc">Three stages: research and planning, grounded writing with bounded editing and regression, image candidates with automatic and manual revision paths.</desc>',
         '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#526174"/></marker></defs>',
         '<rect width="1680" height="1450" fill="white"/>']
    if preview:
        sys.path.insert(0,str(ROOT));from PIL import Image,ImageDraw
        from geo_workbench.images import label_font
        im=Image.new('RGB',(W,H),'white');draw=ImageDraw.Draw(im)
    for x in (30,590,1150):
        svg.append(f'<rect x="{x}" y="98" width="500" height="1290" rx="18" fill="#fafbfe" stroke="#dce3ec"/>')
        if preview:draw.rounded_rectangle((x,98,x+500,1388),radius=18,fill='#fafbfe',outline='#dce3ec')
    for points,label in EDGES:
        svg.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in points)+'" fill="none" stroke="#526174" stroke-width="2.5" marker-end="url(#arrow)"/>')
        if preview:
            draw.line(points,fill='#526174',width=3);(x0,y0),(x1,y1)=points[-2:];angle=math.atan2(y1-y0,x1-x0)
            draw.polygon([(x1,y1),(x1-11*math.cos(angle-.45),y1-11*math.sin(angle-.45)),(x1-11*math.cos(angle+.45),y1-11*math.sin(angle+.45))],fill='#526174')
        if label:
            x,y=points[0];labels.append((x+12,y+28,label,16))
    for ident,x,y,w,h,lines,kind in NODES:
        fill,stroke=COLORS[kind];svg.append(f'<g id="{ident}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}"/>')
        if preview:draw.rounded_rectangle((x,y,x+w,y+h),radius=12,fill=fill,outline=stroke,width=2)
        for index,line in enumerate(lines):
            size=21 if index==0 and w>300 else 18
            tx=x+16;ty=y+29+index*27
            svg.append(f'<text x="{tx}" y="{ty}" font-family="Segoe UI, PingFang SC, Microsoft YaHei, sans-serif" font-size="{size}" fill="#17283e">{html.escape(line)}</text>')
            if preview:
                font=label_font(size)
                if draw.textlength(line,font=font)>w-28:raise ValueError('Diagram text exceeds box: '+ident)
                draw.text((tx,ty-size-1),line,font=font,fill='#17283e')
        svg.append('</g>')
    for x,y,text,size in labels:
        svg.append(f'<text x="{x}" y="{y}" font-family="Segoe UI, PingFang SC, Microsoft YaHei, sans-serif" font-size="{size}" fill="#263b56">{html.escape(text)}</text>')
        if preview:draw.text((x,y-size-1),text,font=label_font(size),fill='#263b56')
    svg.append('</svg>');(ROOT/'docs'/'agent-workflow.svg').write_text('\n'.join(svg),encoding='utf-8')
    if preview:
        (ROOT/'.release').mkdir(exist_ok=True);im.save(ROOT/'.release'/'agent-workflow-preview.png')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--preview',action='store_true')
    render(parser.parse_args().preview)
