// Nonvacuity covers for the tempo_core assertions (formal/core_cover.sby).
// Included into a generated copy of tempo_core.v by core_nv_gen.sh,
// so the design source stays untouched. Threads start only on a host SPI
// control command (two bytes on the host pins), so every cover here needs
// the solver to drive the host port first.
`ifdef FORMAL
  always @(posedge clk) begin
    if (f_past && rst_n && $past(rst_n)) begin
`ifndef CORE_NV_HOST
      // a thread's registers and flags do change in its own execute cycle
      cover($past(te) == 2'd1 && regs[1*8+1] != $past(regs[1*8+1]));
      cover($past(te) == 2'd2 && fz[2] != $past(fz[2]));
      // PCs advance under thread control
      cover($past(te) == 2'd3 && pc[3] != $past(pc[3]) && !$past(hp_pc_v));
      // the mailbox bound is reached, and HPUT commits right below it
      cover(t2h_n == 3'd4);
      cover(ex_commit && !ldi_phase[te] && op == OP_SYS && fn6 == SYS_HPUT && t2h_n == 3'd3);
`else
      // host-driven exceptions and precondition events
      cover($past(hp_pc_v) && $past(hp_pc_t) == 2'd2 && $past(te) != 2'd2 && pc[2] != $past(pc[2]));
      cover(hp_h2t_v);
      cover(ex_commit && !ldi_phase[te] && op == OP_SYS && fn6 == SYS_HGET);
      cover(hp_t2h_pop);
`endif
    end
  end
`endif
