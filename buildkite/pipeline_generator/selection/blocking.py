"""Block step decision logic."""
from .filtering import should_run_step


def should_block_step(step, config, is_torch_nightly_group: bool = False) -> bool:
    """
    Determine if a step should be behind a block (manual trigger).
    
    Matches jinja logic from lines 508-530 and 600-621.
    NOTE: Torch nightly tests have DIFFERENT blocking logic in torch nightly GROUP!
    
    Args:
        step: The test step
        config: Pipeline configuration
        is_torch_nightly_group: True if being called for torch nightly group, False for regular pipeline
    """
    # In fastcheck mode, fast_check tests are NEVER blocked
    from ..utils.constants import PipelineMode
    if config.pipeline_mode == PipelineMode.FASTCHECK and step.fast_check:
        return False
    
    # For steps in the TORCH NIGHTLY GROUP (lines 600-621)
    if is_torch_nightly_group:
        # Torch nightly group has special blocking logic
        # Start with blocked=1
        blocked = True
        
        # If nightly mode, unblock
        if config.nightly:
            blocked = False
        
        # If source dependencies exist and match file changes, unblock
        if step.source_file_dependencies:
            for source_file in step.source_file_dependencies:
                for changed_file in config.list_file_diff:
                    if source_file in changed_file:
                        blocked = False
                        break
        else:
            # No dependencies means always unblocked
            blocked = False
        
        # ALSO block if optional and not nightly (line 617)
        if step.optional and not config.nightly:
            return True
        
        return blocked
    
    # For regular pipeline steps (lines 508-530) - includes tests with torch_nightly=True!
    if True:  # Always use regular logic for main pipeline

        # Check if blocked due to file dependencies  
        if not should_run_step(step, config):
            return True
        
        # Check if blocked due to being optional (independent of run_all!)
        if step.optional and not config.nightly:
            return True
        
        return False

