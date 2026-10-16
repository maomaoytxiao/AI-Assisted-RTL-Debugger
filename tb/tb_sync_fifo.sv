// tb_sync_fifo.sv - self-checking directed testbench for sync_fifo
//
// Icarus Verilog 10.3 compatible: no classes, queues, interfaces, string
// variables, or concurrent assertions. A cycle-accurate reference model
// (array + head/tail/count) predicts every output; every cycle is checked.
//
// Timing convention:
//   - stimulus is applied at the falling edge (setup well before posedge)
//   - combinational outputs (full/empty/count/overflow/underflow) are
//     checked just before the rising edge against the model's pre-edge state
//   - registered results (rd_data, new count/flags) are checked 1ns after
//     the rising edge against the model's post-edge state

`timescale 1ns/1ps

`ifndef VCD_FILE
  `define VCD_FILE "build/sync_fifo.vcd"
`endif

module tb_sync_fifo;

    localparam WIDTH  = 8;
    localparam DEPTH  = 8;
    localparam AW     = 3;
    localparam PERIOD = 10;

    // ------------------------------------------------------------------
    // DUT signals
    // ------------------------------------------------------------------
    reg              clk     = 1'b0;
    reg              rst_n   = 1'b0;
    reg              wr_en   = 1'b0;
    reg  [WIDTH-1:0] wr_data = {WIDTH{1'b0}};
    reg              rd_en   = 1'b0;
    wire [WIDTH-1:0] rd_data;
    wire             full, empty, overflow, underflow;
    wire [AW:0]      count;

    always #(PERIOD/2) clk = ~clk;

    sync_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (
        .clk      (clk),
        .rst_n    (rst_n),
        .wr_en    (wr_en),
        .wr_data  (wr_data),
        .full     (full),
        .overflow (overflow),
        .rd_en    (rd_en),
        .rd_data  (rd_data),
        .empty    (empty),
        .underflow(underflow),
        .count    (count)
    );

    // Expose DUT memory words as plain wires so they appear in the VCD
    // (Icarus does not dump unpacked arrays via $dumpvars on the scope).
    genvar gi;
    generate
        for (gi = 0; gi < DEPTH; gi = gi + 1) begin : g_mem
            wire [WIDTH-1:0] word = dut.mem[gi];
        end
    endgenerate

    // ------------------------------------------------------------------
    // Reference model
    // ------------------------------------------------------------------
    reg [WIDTH-1:0] m_mem [0:DEPTH-1];
    integer         m_head;
    integer         m_tail;
    integer         m_count;
    reg [WIDTH-1:0] m_rd_data;

    // Scoreboard / bookkeeping
    integer         errors       = 0;
    integer         checks       = 0;
    integer         test_errors0 = 0;
    integer         n_tests      = 0;
    integer         n_failed     = 0;
    integer         n_wr_acc     = 0;
    integer         n_rd_acc     = 0;
    integer         n_ovf        = 0;
    integer         n_unf        = 0;
    integer         n_both       = 0;
    reg [8*32-1:0]  cur_test;
    reg [WIDTH-1:0] seq          = 8'h00;   // unique data generator

    task model_reset;
        begin
            m_head    = 0;
            m_tail    = 0;
            m_count   = 0;
            m_rd_data = {WIDTH{1'b0}};
        end
    endtask

    // Generic 4-state compare
    task check(input [8*16-1:0] what, input [31:0] got, input [31:0] exp);
        begin
            checks = checks + 1;
            if (got !== exp) begin
                errors = errors + 1;
                $display("  ERROR @%0t [%0s] %0s: got 0x%0h expected 0x%0h",
                         $time, cur_test, what, got, exp);
            end
        end
    endtask

    // Check all outputs against the model's current state
    task check_state;
        begin
            check("rd_data", rd_data, m_rd_data);
            check("count",   count,   m_count);
            check("full",    full,    (m_count == DEPTH));
            check("empty",   empty,   (m_count == 0));
        end
    endtask

    // ------------------------------------------------------------------
    // One clock cycle of stimulus + checking
    // ------------------------------------------------------------------
    task step(input w, input [WIDTH-1:0] d, input r);
        reg exp_wr, exp_rd;
        begin
            @(negedge clk);
            wr_en   = w;
            wr_data = d;
            rd_en   = r;
            #1;
            // Pre-edge: combinational outputs reflect state before the edge
            exp_wr = w && (m_count < DEPTH);
            exp_rd = r && (m_count > 0);
            check_state;
            check("overflow",  overflow,  (w && m_count == DEPTH));
            check("underflow", underflow, (r && m_count == 0));

            @(posedge clk);
            #1;
            // Model update (read first: it uses the pre-edge head entry)
            if (exp_rd) begin
                m_rd_data = m_mem[m_head];
                m_head    = (m_head + 1) % DEPTH;
                m_count   = m_count - 1;
                n_rd_acc  = n_rd_acc + 1;
            end
            if (exp_wr) begin
                m_mem[m_tail] = d;
                m_tail        = (m_tail + 1) % DEPTH;
                m_count       = m_count + 1;
                n_wr_acc      = n_wr_acc + 1;
            end
            if (w && !exp_wr) n_ovf  = n_ovf + 1;
            if (r && !exp_rd) n_unf  = n_unf + 1;
            if (exp_wr && exp_rd) n_both = n_both + 1;
            // Post-edge: registered outputs
            check_state;
        end
    endtask

    // Convenience wrappers
    task push;   begin seq = seq + 1; step(1'b1, seq, 1'b0); end endtask
    task pop;    begin step(1'b0, 8'h00, 1'b1); end endtask
    task idle;   begin step(1'b0, 8'h00, 1'b0); end endtask
    task pushpop; begin seq = seq + 1; step(1'b1, seq, 1'b1); end endtask

    task push_n(input integer n); integer i; begin for (i = 0; i < n; i = i + 1) push;    end endtask
    task pop_n (input integer n); integer i; begin for (i = 0; i < n; i = i + 1) pop;     end endtask

    // Apply reset at the falling edge, hold for 2 cycles, release at a
    // falling edge. Checks outputs while reset is asserted.
    task apply_reset;
        begin
            @(negedge clk);
            wr_en = 1'b0; rd_en = 1'b0; wr_data = {WIDTH{1'b0}};
            rst_n = 1'b0;
            model_reset;
            #1 check_state;                // async: takes effect immediately
            repeat (2) @(posedge clk);
            #1 check_state;
            @(negedge clk);
            rst_n = 1'b1;
            #1 check_state;
        end
    endtask

    task begin_test(input [8*32-1:0] name);
        begin
            cur_test     = name;
            test_errors0 = errors;
            n_tests      = n_tests + 1;
            $display("[%0t] TEST %0d: %0s", $time, n_tests, name);
        end
    endtask

    task end_test;
        begin
            if (errors == test_errors0)
                $display("         -> PASS");
            else begin
                n_failed = n_failed + 1;
                $display("         -> FAIL (%0d errors)", errors - test_errors0);
            end
        end
    endtask

    // ------------------------------------------------------------------
    // Directed tests
    // ------------------------------------------------------------------
    integer k;

    initial begin
        $dumpfile(`VCD_FILE);
        $dumpvars(0, tb_sync_fifo);

        model_reset;
        cur_test = "init";

        // 1. Reset ------------------------------------------------------
        begin_test("reset");
        apply_reset;
        idle; idle;
        end_test;

        // 2. Fill -------------------------------------------------------
        begin_test("fill");
        push_n(DEPTH);                     // full asserted after 8th write
        idle;
        end_test;

        // 3. Drain ------------------------------------------------------
        begin_test("drain");
        pop_n(DEPTH);                      // in-order data, empty at end
        idle;
        end_test;

        // 4. Overflow requests ------------------------------------------
        begin_test("overflow");
        push_n(DEPTH);
        for (k = 0; k < 3; k = k + 1)
            step(1'b1, 8'hE0 + k, 1'b0);   // dropped; must not corrupt data
        pop_n(DEPTH);                      // original contents intact
        end_test;

        // 5. Underflow requests -----------------------------------------
        begin_test("underflow");
        pop_n(3);                          // dropped; rd_data holds value
        idle;
        push; pop;                         // FIFO still works afterwards
        pop;                               // underflow again
        end_test;

        // 6. Pointer wraparound -----------------------------------------
        begin_test("wraparound");
        // Partial cycles shift the pointers across the 8-entry boundary
        for (k = 0; k < 5; k = k + 1) begin
            push_n(5);
            pop_n(5);
        end
        // Fill/drain starting from a non-zero offset
        push_n(3); pop_n(3);
        push_n(DEPTH);                     // full with wrapped wr_ptr
        step(1'b1, 8'hEE, 1'b0);           // overflow while wrapped
        pop_n(DEPTH);
        end_test;

        // 7. Simultaneous read/write ------------------------------------
        begin_test("simultaneous_rw");
        // 7a: empty + rd + wr -> only write accepted, no fall-through
        pushpop;
        // 7b: mid-level + rd + wr -> both accepted, count constant
        push_n(3);                         // count = 4
        for (k = 0; k < 12; k = k + 1) pushpop;
        // 7c: full + rd + wr -> only read accepted
        push_n(DEPTH - 4);                 // count = 8 (full)
        pushpop;                           // count -> 7
        pushpop;                           // count stays 7 (both accepted)
        // 7d: streaming at count = 1
        pop_n(DEPTH - 2);                  // count = 1
        for (k = 0; k < 16; k = k + 1) pushpop;
        pop; pop;                          // drain; second pop underflows
        // 7e: full + rd only + wr only alternation around the boundary
        push_n(DEPTH);
        pop; push; pushpop; pop_n(DEPTH);
        end_test;

        // 8. Reset during operation -------------------------------------
        begin_test("reset_mid_operation");
        push_n(5); pop;                    // count = 4, rd_data non-zero
        apply_reset;                       // flushes contents, rd_data = 0
        pop;                               // underflow: old data gone
        push; push; pop; pop; pop;
        end_test;

        // Summary -------------------------------------------------------
        idle;
        $display("");
        $display("==================== SUMMARY ====================");
        $display(" tests run        : %0d", n_tests);
        $display(" tests failed     : %0d", n_failed);
        $display(" checks performed : %0d", checks);
        $display(" errors           : %0d", errors);
        $display(" accepted writes  : %0d   accepted reads : %0d", n_wr_acc, n_rd_acc);
        $display(" dropped writes   : %0d   dropped reads  : %0d", n_ovf, n_unf);
        $display(" same-cycle rd+wr : %0d", n_both);
        $display("=================================================");
        if (errors == 0 && n_failed == 0)
            $display("TEST PASSED");
        else
            $display("TEST FAILED");
        $finish;
    end

    // Watchdog
    initial begin
        #100000;
        $display("ERROR: watchdog timeout");
        $display("TEST FAILED");
        $finish;
    end

endmodule
