"""Check the lane miter's covers with two deterministic RTL traces.

Run from any directory: python formal/check_miter_trace.py
Requires Icarus Verilog. Outputs go to runs/formal_miter_trace/.

This is a concrete reachability check, not a solver witness or safety proof.
The generated simulation copy binds the symbolic inputs to a permitted tape
and push schedule, and instruments the original cover conditions. Its
assertions and assumptions remain active. The negative trace restores the
old TX/TX/LW strong-cover condition; the current TX/LW/TX trace must reach
both covers. No design source is modified.
An invalid-assumption trace must also be rejected despite completing both
covers, so a disabled runtime constraint check cannot silently pass.
"""

from pathlib import Path
import re, subprocess, json, hashlib
root=Path(__file__).resolve().parents[1]
output=root/'runs'/'formal_miter_trace'
output.mkdir(parents=True,exist_ok=True)
source_files=(root/'formal'/'lane_miter.sv',root/'src'/'tempo_lane.v',Path(__file__).resolve())
source_bytes={p:p.read_bytes() for p in source_files}
source_hashes={str(p.relative_to(root)):hashlib.sha256(data).hexdigest() for p,data in source_bytes.items()}
source=source_bytes[root/'formal'/'lane_miter.sv'].decode()
lane_snapshot=output/'tempo_lane.v'
lane_snapshot.write_bytes(source_bytes[root/'src'/'tempo_lane.v'])
fixed=source
old=source.replace('`KIND(3) == 3\'d3 && `KIND(4) == 3\'d0', '`KIND(3) == 3\'d0 && `KIND(4) == 3\'d3')
assert old != source

def packed(values,width):
    return sum(v << (i*width) for i,v in enumerate(values))

def run(label, text, middle_lw, expect_hit):
    kinds=[3,4,0,3,0,2] if middle_lw else [3,4,0,0,3,2]
    data=[8,20,0,0xC000,15,0x91] if middle_lw else [8,20,0,15,0xC000,0x91]
    regs=[15,0,0,5,0,0] if middle_lw else [15,0,0,0,5,0]
    fields={'kind_v':(3,kinds),'data_v':(16,data),'n_v':(5,[1,1,4,1 if middle_lw else 4,4 if middle_lw else 1,1]),'fl_v':(3,[0]*6),'rg_v':(4,regs)}
    assert text.count('module lane_miter (input wire clk);')==1
    text=text.replace('module lane_miter (input wire clk);','module lane_miter (input wire clk, input wire [15:0] pins_in, input wire go_a, go_b);')
    text, replaced=re.subn(r'^  \(\* anyseq \*\).*\n','',text,flags=re.M)
    assert replaced==2
    for name,(width,values) in fields.items():
        text, replaced=re.subn(r'(\(\* anyconst \*\) reg \[[^\n]+?\]\s+'+name+r')\s*;', lambda m: m[1]+f" = {width*6}'h{packed(values,width):x};",text)
        assert replaced==1, name
    # Constant initializers do not trigger always @* at time zero. Formal
    # constraints are active even then; always_comb preserves that here.
    assert text.count('  always @* begin\n    // entry 0')==1
    text=text.replace('  always @* begin\n    // entry 0', '  always_comb begin\n    // entry 0')
    # Equivalent bit selection accepted by Icarus as well as Yosys.
    assert text.count('`FL(i)[0]')==1
    text=text.replace('`FL(i)[0]', 'fl_v[i*3]')
    counter=[0]
    def cover(m):
        idx=counter[0];counter[0]+=1
        return 'if ('+m[1]+f') hit{idx} = 1\'b1;'
    text=re.sub(r'cover\((.*?)\);',cover,text,flags=re.S)
    assert counter[0]==2
    text=text.replace('  integer i;', "  reg hit0 = 0, hit1 = 0;\n  integer i;")
    out=output/label
    sv=out.with_suffix('.sv');sv.write_text(text)
    tb=out.with_name(out.name+'_tb.sv')
    tb.write_text('''module tb;
reg clk=0, ga=0, gb=0;
integer ta[0:5],tbm[0:5],k;
lane_miter dut(clk,16'd0,ga,gb);
always #5 clk=~clk;
always @(negedge clk) begin
 ga=(dut.pa<6 && dut.now==ta[dut.pa]);
 gb=(dut.pb<6 && dut.now==tbm[dut.pb]);
end
initial begin
 ta[0]=1; ta[1]=3; ta[2]=5; ta[3]=20; ta[4]=21; ta[5]=29;
 tbm[0]=1; tbm[1]=3; tbm[2]=5; tbm[3]=21; tbm[4]=22; tbm[5]=30;
 for(k=0;k<60;k=k+1) begin
  @(posedge clk); #1;
  if(dut.rst_n && (dut.bad_a || dut.bad_b)) $fatal(1,"timing failure");
  if(dut.started_a && dut.started_b &&
     (dut.da!=dut.db || dut.aa!=dut.ab || dut.ca!=dut.cb || dut.oa!=dut.ob))
   $fatal(1,"drive mismatch");
  $display("TRACE now=%0d pa=%0d pb=%0d da=%b db=%b run=%b%b low=%b high=%b skew=%b",dut.now,dut.pa,dut.pb,dut.da,dut.db,dut.runa,dut.runb,dut.payload_low_seen,dut.payload_high_seen,dut.payload_skew_seen);
 end
 if(!dut.hit0 || dut.hit1!=EXPECT || dut.pa!=6 || dut.pb!=6 || !dut.ida || !dut.idb)
  $fatal(1,"cover mismatch simple=%b strong=%b",dut.hit0,dut.hit1);
 $display("RESULT simple=%b strong=%b",dut.hit0,dut.hit1);
 $finish;
end
endmodule
'''.replace('EXPECT',"1'b1" if expect_hit else "1'b0"))
    exe=out.with_suffix('.vvp')
    args=['iverilog','-g2012','-gassertions','-s','tb','-o',str(exe),str(lane_snapshot),str(sv),str(tb)]
    comp=subprocess.run(args,capture_output=True,text=True,timeout=30)
    assert comp.returncode==0,comp.stderr
    sim=subprocess.run(['vvp',str(exe)],capture_output=True,text=True,timeout=30)
    out.with_suffix('.log').write_text(sim.stdout+sim.stderr)
    assert sim.returncode==0,sim.stdout+sim.stderr
    assert 'ERROR' not in sim.stdout+sim.stderr,sim.stdout+sim.stderr
    return {'label':label,'strong_cover_reached':expect_hit,'returncode':sim.returncode,'log':str(out.with_suffix('.log'))}
results=[run('old_tape',old,False,False),run('fixed_tape',fixed,True,True)]
invalid=source.replace("assume(`NB(i) >= 5'd1", "assume(`NB(i) >= 5'd2")
assert invalid!=source
try:
    run('violated_assumption',invalid,True,True)
except AssertionError as error:
    # n is unused by LW, so the RTL and both covers still complete. Only
    # evaluating the changed formal constraint must reject this trace.
    message=str(error)
    assert 'ERROR:' in message and 'Time: 0' in message and 'RESULT simple=1 strong=1' in message, message
else:
    raise AssertionError('Checker accepted a violated formal assumption')
assert all(p.read_bytes()==data for p,data in source_bytes.items()), 'Source changed during trace check'
(output/'results.json').write_text(json.dumps({'evidence':'Deterministic two-copy RTL trace with constant tape/push schedules; not a solver-generated witness or safety proof','source_sha256':source_hashes,'sources_unchanged_after':True,'invalid_assumption_rejected':True,'results':results},indent=2)+'\n')
print(json.dumps(results,indent=2))
