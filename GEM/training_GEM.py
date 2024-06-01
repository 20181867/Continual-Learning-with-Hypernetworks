import sys
sys.path.append(r'C:\Users\20181867\Documents\Schoolbestanden\Thesis\Code')
import tensorflow as tf
import numpy as np
from tqdm import tqdm
from itertools import islice
from Training_blanco.training import fill_with_none
import copy
import random


" THE GEM SPECIFIC FUNTIONS"
# Function to update memory buffer with representations of data (in form of target network weights) encountered during previous tasks
def update_memory_buffer(memory_buffer, grads):

    #check if and if so where there is space to place the new gradient
    for count, previous_element in enumerate(memory_buffer):
        #fill in empty space
        if previous_element == 0:
            memory_buffer[count] = copy.deepcopy(grads)
            break
        #there is no empty space left, replace a random gradient space:
        if previous_element != 0 and count == len(memory_buffer) -1:
            memory_buffer[random.randint(0, len(memory_buffer)-1)] = copy.deepcopy(grads)
    
    return memory_buffer

# Function to apply GEM regularization to gradients
def apply_gem_regularization(grads, memory_buffer, lrate, trainables, Average_GEM, margin):

    #in Avarage GEM, select a sample of the memory buffer to update with
    if Average_GEM[0]:
        index_filled_memory = []
        for count, old_gradients in enumerate(memory_buffer):
            if old_gradients == 0:
                continue
            else:
                index_filled_memory.append(count)
        
        if (not len(index_filled_memory) == 0) and (Average_GEM[1] < len(index_filled_memory)+1):
            copy_full_memory = memory_buffer
            A_memory_buffer = []
            A_sample = random.sample(index_filled_memory, Average_GEM[1])
            for sample in A_sample:
                A_memory_buffer.append(memory_buffer[sample])
            memory_buffer = copy.deepcopy(A_memory_buffer)

    result = [0 for grad in grads]
    grad_scaling = [1,0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1,0.01, 0.001, 0.0001, 0.00001,0]
    for count_grad_scale, grad_scale in enumerate(grad_scaling):
        redo_calculations = False

        for old_gradients in memory_buffer:
            #if empty storage space
            if old_gradients == 0:
                continue

            #if filled with old gradient
            for count, (current_grad, old_grad) in enumerate(zip(grads, old_gradients)):
                #for CIL, the current gradient might deviate in shape. Solution: do every possible gradient, except the last layer in the hypernetwork if the shape does not match.
                if current_grad.shape != old_grad.shape:
                    continue
                #calculate result using given formula
                if len(old_grad)> 1:
                    #if matrices:
                    try:
                        numerator = tf.matmul(tf.transpose(current_grad), old_grad)- margin
                    #if vectors:
                    except:
                        numerator =current_grad* old_grad- margin
                    devidor = tf.square(tf.norm(old_grad))
                    # to prevent devision by zero:
                    if devidor == 0:
                        grad_sum = 0
                    else:
                        grad_sum = tf.divide(numerator, devidor)

                    try:
                        result_sum =  tf.reduce_sum(tf.maximum(0, grad_sum)) *old_grad
                    except:
                        result_sum =  tf.cast(tf.reduce_sum(tf.maximum(0, grad_sum)), tf.float32) *tf.cast(old_grad, tf.float32)

                    #if matrices:
                    try:
                        if tf.reduce_any(tf.greater(tf.matmul(result[count]*grad_scale, tf.transpose(current_grad)), margin)):
                            redo_calculations = True
                            break
                    except:
                        if tf.reduce_any(tf.greater((result[count]*grad_scale) * current_grad, margin)):
                            redo_calculations = True
                            break

                    #add to the correct trainable parameter
                    result[count] += result_sum*grad_scale
            
            if redo_calculations:
                break
        
        if not redo_calculations:
            break

        if count_grad_scale == len(grad_scaling)- 1:
            print('Warning: margin cannot be reached')

    if not tf.is_tensor(result[0]):
        print('-'*20 + 'NO (A)GEM REGULARIZATION' + '-'*20)
        gem_gradient = grads
    else:
        gem_gradient = []
        for grad, result_ in zip(grads, result):
            gem_gradient.append(grad - result_)


    for count, trainable in enumerate(trainables):
        trainables[count].assign(trainable - lrate * gem_gradient[count])
    
    if Average_GEM[0]:
        if (not len(index_filled_memory) == 0) and (Average_GEM[1] < len(index_filled_memory)+1):
            memory_buffer = copy.deepcopy(copy_full_memory)

    memory_buffer = update_memory_buffer(memory_buffer, grads)

    return trainables, memory_buffer

" THE (MODIFIED) TRAINING FUNTIONS"
@tf.function
def train_step(task_embedding, x, y, model, loss_fun, trainables, optimizer, memory_buffer, storage_capacity,
               class_incremental_case, use_unique_task_embedding,
               entropy_values, increase_classes = False,  increase_classes_allowed = False, final_soft_max_layer = False, treshold_CIL = 0, Average_GEM = False, margin = 0, chunk_number=(None, None)):

    needed_aid = False

    with tf.GradientTape() as tape:
        outs, old_rep = model([task_embedding, x, increase_classes])
        try:
            loss = loss_fun(y, outs)
        except:
            zeros_tensor = tf.zeros(int(y) - int(tf.size(outs).numpy() -1 ))
            corrected_outs = tf.concat([outs, zeros_tensor], axis=0)
            loss = loss_fun(y, corrected_outs)
            needed_aid = True

        if model.losses:
            loss += tf.add_n(model.losses)

    #if we are in the class incremental learning senario
    if class_incremental_case[0] and increase_classes_allowed:

        global CIL_memory_buffer #
        epsilon = 1e-10  # Small epsilon value to prevent zero probabilities
        if not final_soft_max_layer:
            probabilities = tf.nn.softmax(np.squeeze(outs)) + epsilon
        else:
            probabilities = outs+ epsilon
        
        if len(probabilities.shape) == 1:
            entropy = (-tf.reduce_sum(probabilities * tf.math.log(probabilities), axis=0))/ len(probabilities)
        else:
            entropy = (-tf.reduce_sum(probabilities * tf.math.log(probabilities), axis=1))/ len(probabilities[1])
   
        entropy_values.append(entropy.numpy())
        #calculate similarity with previously stored te's
        x_for_CIL = np.reshape(x, [-1])

        similarity = 0
        for element in CIL_memory_buffer:
            try:
                if element == 0:
                    break
            except:
                similarity = (similarity + tf.reduce_sum(element * x_for_CIL) / (tf.norm(element)* tf.norm(x_for_CIL)))/2

        if class_incremental_case[3][0]: #if exponential moving average
            smoothed_entropy_values = exponential_moving_average(entropy_values, class_incremental_case[3][1])
            if similarity == 0:
                total_score = smoothed_entropy_values[-1]
            else:                
                try:
                    total_score = tf.minimum(class_incremental_case[5]* chunk_number[1], 1)* similarity + tf.maximum(1- (class_incremental_case[5]* chunk_number[1]), 0)* smoothed_entropy_values[-1]
                except:
                    total_score = tf.minimum(class_incremental_case[5]* chunk_number[1], 1)* similarity.numpy() + tf.maximum(1- (class_incremental_case[5]* chunk_number[1]), 0)* smoothed_entropy_values[-1]
        else:
            if similarity == 0:
                total_score = entropy
            else:
                #Score(x) = min(λc, 1)Prob(x) + max(1 − λc, 0)Ent(x)
                try:
                    total_score = tf.minimum(class_incremental_case[5]* chunk_number[1], 1)* similarity + tf.maximum(1- (class_incremental_case[5]* chunk_number[1]), 0)* entropy
                except:
                    total_score = tf.minimum(class_incremental_case[5]* chunk_number[1], 1)* similarity.numpy() + tf.maximum(1- (class_incremental_case[5]* chunk_number[1]), 0)* entropy

        if total_score> treshold_CIL: # a high entropy is encountered (higher then treshold)
            new_class_detected = True
        else:
            new_class_detected = False

        #report to researcher
        if not final_soft_max_layer:
            print(' ')
            print('Due to these probabilities: {}'.format(tf.round(np.squeeze(tf.nn.softmax(np.squeeze(outs))* 100)/100)))
            print('The entropy is: {}'.format(round(entropy.numpy(), 2)))
            print('The similarity score is: {}'.format(similarity))
            print('Therefore the score is: {} \n'.format(total_score))

        else:
            print(' ')
            print('Due to these probabilities: {}'.format((tf.round(outs * 100) / 100)))
            print('The entropy is: {}'.format(round(entropy.numpy(), 2)))
            print('The similarity score is: {}'.format(similarity))
            print('Therefore the score is: {} \n'.format(total_score))

        #if indeed a new class is detected and the model is allowed to adjust to this
        if new_class_detected and increase_classes_allowed:
            #check is buffer should be cleaned before adding to it, by counting empty space left:
            cleaning = True
            for count, element in enumerate(CIL_memory_buffer):
                try:
                    if element == 0:
                        CIL_memory_buffer[count] = x_for_CIL
                        cleaning = False
                        break
                except:
                    continue
            if cleaning:
                print("New class detected! Adjusting model...")
                CIL_memory_buffer = [0 for i in range(0,class_incremental_case[6])]
                CIL_memory_buffer[0] = x_for_CIL
                
                #recursion
                outs, entropy_values, optimizer, increase_classes, trainables, memory_buffer, chunk_number = train_step(task_embedding, x, y, model, loss_fun, trainables, optimizer,  memory_buffer, storage_capacity, (False, class_incremental_case[1], class_incremental_case[2], (False, 0), class_incremental_case[4]), use_unique_task_embedding,
                    entropy_values, increase_classes = True,  increase_classes_allowed = True, final_soft_max_layer = final_soft_max_layer, treshold_CIL = treshold_CIL, Average_GEM = Average_GEM, margin =margin, chunk_number = chunk_number)
                return outs, entropy_values, optimizer, increase_classes, trainables, memory_buffer, chunk_number
        
    #if adding a class, you must redefine the trainables and the optimizer
    if increase_classes:
        if use_unique_task_embedding:
            trainables = model.trainable_variables
        else:
            trainables = model.trainable_variables + [task_embedding]

        optimizer = tf.keras.optimizers.Adam(learning_rate=optimizer.learning_rate)
        optimizer.build(trainables)

    grads = tape.gradient(loss, trainables)
    trainables, memory_buffer = apply_gem_regularization(grads, memory_buffer, optimizer.learning_rate, trainables, Average_GEM, margin)
    #optimizer.apply_gradients(zip(grads, trainables))

    if needed_aid:
        outs = corrected_outs
    return outs, entropy_values, optimizer, increase_classes, trainables, memory_buffer, chunk_number


@tf.function
def test_step(model, task_embedding, x, y):
    outs, _ = model([task_embedding,x, False])
    return outs

def exponential_moving_average(entropies, alpha=0.2):
    """Compute the exponential moving average over entropy values."""
    ema = [entropies[0]]
    for i in range(1, len(entropies)):
        ema.append(alpha * entropies[i] + (1 - alpha) * ema[-1])
    return ema


def create_my_model(which_model, embedding_dim, n_chunks, hnet_hidden_dims,
                    inner_net_dims, dropout_rate, use_unique_task_embedding, 
                    convolution_layers, l2reg, initialize_TE_with_zero_bias,
                    final_soft_max_layer):
    
    if which_model[0] == 'Dense':
        from Models.Hypermodel import create
        model = create(embedding_dim = embedding_dim,
                        n_chunks = n_chunks,
                        hnet_hidden_dims = hnet_hidden_dims,
                        inner_net_dims = inner_net_dims,
                        dropout_rate = dropout_rate,
                        use_unique_task_embedding = use_unique_task_embedding, 
                        convolution_layers=convolution_layers,
                        l2reg=l2reg,
                        initialize_with_zero_bias = initialize_TE_with_zero_bias,
                        final_soft_max_layer = final_soft_max_layer)
    elif which_model[0] == 'DRNN':
        #('DRNN', depency_preservation_between_chunks, dilation_rate)
        from Models.Hypermodel_DRNN import create
        model = create(embedding_dim=embedding_dim,
                        n_chunks=n_chunks,
                        hnet_hidden_dims=hnet_hidden_dims,
                        inner_net_dims=inner_net_dims,
                        #l2reg L2 regularization, also known as weight decay, penalizes large weights in the model by adding a term to the loss function that is proportional to the square of the weight values. This helps prevent overfitting by discouraging overly complex models.
                        l2reg=l2reg,
                        dropout_rate = dropout_rate,
                        use_unique_task_embedding = use_unique_task_embedding,
                        convolution_layers=convolution_layers, 
                        dilation_rate = which_model[2],
                        depency_preservation_between_chunks = which_model[1],
                        final_soft_max_layer = final_soft_max_layer,
                        initialize_TE_with_zero_bias = initialize_TE_with_zero_bias)
    elif which_model[0] == 'ECHO':
        #('ECHO', depency_preservation_between_chunks, reservoir_size, spectral_radius, sparsity)
        from Models.Hypermodel_ECHO import create
        model = create(embedding_dim=embedding_dim,
                        n_chunks=n_chunks,
                        hnet_hidden_dims=hnet_hidden_dims,
                        inner_net_dims=inner_net_dims,
                        l2reg=l2reg,
                        dropout_rate = dropout_rate,
                        use_unique_task_embedding = use_unique_task_embedding,
                        convolution_layers=convolution_layers, 
                        depency_preservation_between_chunks = which_model[1],
                        reservoir_size = which_model[2], spectral_radius = which_model[3], sparsity=which_model[4],
                        final_soft_max_layer = final_soft_max_layer,
                        initialize_TE_with_zero_bias = initialize_TE_with_zero_bias)
    elif which_model[0] == 'GRU':
        #('GRU', depency_preservation_between_chunks)
        from Models.Hypermodel_GRU import create_GRU
        model = create_GRU(embedding_dim=embedding_dim,
                        n_chunks=n_chunks,
                        hnet_hidden_dims=hnet_hidden_dims,
                        inner_net_dims=inner_net_dims,
                        l2reg=l2reg,
                        dropout_rate = dropout_rate,
                        use_unique_task_embedding = use_unique_task_embedding,
                        convolution_layers=convolution_layers, 
                        depency_preservation_between_chunks = which_model[1],
                        final_soft_max_layer = final_soft_max_layer,
                        initialize_TE_with_zero_bias = initialize_TE_with_zero_bias)
    elif which_model[0] == 'LSTM':
        #('GRU', depency_preservation_between_chunks)
        from Models.Hypermodel_LSTM import create_LSTM
        model = create_LSTM(embedding_dim=embedding_dim,
                            n_chunks=n_chunks,
                            hnet_hidden_dims=hnet_hidden_dims,
                            inner_net_dims=inner_net_dims,
                            l2reg=l2reg,
                            dropout_rate = dropout_rate,
                            use_unique_task_embedding = use_unique_task_embedding,
                            convolution_layers=convolution_layers, 
                            depency_preservation_between_chunks = which_model[1],
                            final_soft_max_layer = final_soft_max_layer,
                            initialize_TE_with_zero_bias = initialize_TE_with_zero_bias)
    else:
        raise ValueError('Please enter a correct model (Dense, DRNN, ECHO, GRU or LSTM)')

    return model



def run(X_train = [np.random.rand(10, 128, 32, 1)]*3, 
            y_train = [np.random.randint(0, 3, size=(10))]*3,
            X_test = [np.random.rand(5, 128, 32, 1)]*3,
            y_test = [np.random.randint(0, 3, size=(5))]*3,
            optimizer = tf.keras.optimizers.Adam(learning_rate=0.0001), 
            embedding_dim = 100, 
            num_classes =3,
            l2reg = 0, 
            inner_net_dims = (416, 832),
            loss_fun = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True), 
            convolution_layers = [(16, (4,4), 1), (64, (5,5), 16), (64, (3,3), 64), (32, (3,3), 64)],
            use_unique_task_embedding = False,
            hnet_hidden_dims = (100, 50),
            dropout_rate = 0.3,
            n_chunks = 10,
            max_attempts = 3,
            steps_per_epoch = 3,
            validation_accuracy = -1.0,
            storage_capacity = 6,
            class_incremental_case = (True, 0.4, -1, (False, 0.1), (4, 0.5), 0.4, 5),
            initialize_TE_with_zero_bias = True,
            final_soft_max_layer = True,#if soft max is true, the loss function expects NO 'from_logits'
            training_while_testing = False,
            which_model = ('Dense', False, 10, 0.5, 0.5),
            Average_GEM = (True, 3),
            margin = 0.01):

    #setup data collection 
    train_accuracies = []
    test_accuracies = []
    predictions = []
    amount_of_classes = [(num_classes, 0, 0)]

    try:
        inner_net_dims = inner_net_dims + (num_classes,)
    except TypeError:
        inner_net_dims = (inner_net_dims, num_classes)
    n_tasks = len(X_train)
    task_embeddings = [tf.Variable(tf.random.normal([embedding_dim], stddev=1) / 10, trainable=True) for _ in range(n_tasks)]
    memory_buffer = [0 for i in range(0,storage_capacity)]

    #for the class incremental learning scenario
    entropy_values = []
    resettle_timer, resettle_timer_updater = class_incremental_case[4][0], class_incremental_case[4][0]
    treshold_CIL = class_incremental_case[1]
    added_a_class = False
    chunk_number = (False, -class_incremental_case[2])
    global CIL_memory_buffer
    CIL_memory_buffer = [0 for i in range(0, class_incremental_case[6])]


    #create model
    model = create_my_model(which_model, embedding_dim, n_chunks, hnet_hidden_dims,
                    inner_net_dims, dropout_rate, use_unique_task_embedding, 
                    convolution_layers, l2reg, initialize_TE_with_zero_bias,
                    final_soft_max_layer)
    
    if use_unique_task_embedding:
        trainables = model.trainable_variables

    else:
        trainables = model.trainable_variables + task_embeddings
        
    optimizer.build(trainables)  # Build optimizer with all trainable variables

    # if in class incremental case, cap the storage capacity anyway. The storage capacity is the representation size of the first model (without any additional classes)
    # Note: the difference in representation size between a model with zero, one or two additional classes is neglectable in most cases.
    if (class_incremental_case[0]) and (storage_capacity is None):
        _, representations = model([task_embeddings[0], tf.expand_dims(X_train[0][0], axis=0), False])
        storage_capacity = len(representations)

    
    warned_about_AGEM = False

    for i in range(0, n_tasks):
        print('RUNNING TASK {} \n'.format(i+1)+ '-'*30)

        if (not warned_about_AGEM) and (Average_GEM[0]):
            #count filled memory:
            sum_grads = 0
            for old_grads in memory_buffer:
                if isinstance(old_grads, list):
                    sum_grads +=1
            if sum_grads >= Average_GEM[1]:
                print('-'*30 + 'Average GEM has started' + '-'*30)
                warned_about_AGEM= True

        #spread out testing times, with the assumption we have more training than test data
        X_test[i] = fill_with_none(X_test[i], X_train[i][:steps_per_epoch])
        y_test[i] = fill_with_none(y_test[i], y_train[i][:steps_per_epoch])

        #get a unique random task embedding for every task
        task_embedding = task_embeddings[i]

        #task id is the task number
        task_id = int(i)

        #this task embedding is only trainable if the task embedding model is not used
        if not use_unique_task_embedding:
            trainables = model.trainable_variables + [task_embedding]
            if class_incremental_case[0] and added_a_class:
                trainables = model.trainable_variables + task_embeddings
                optimizer = tf.keras.optimizers.Adam(learning_rate=optimizer.learning_rate)
                optimizer.build(trainables)
                trainables = model.trainable_variables + [task_embedding]
        
        #update memory buffer every task
        #memory_buffer = update_memory_buffer(memory_buffer, task_embedding, model, X_train[i], storage_capacity)
        if validation_accuracy is None:
            final_accuracy, train_accuracies,test_accuracies,predictions, amount_of_classes, entropy_values, optimizer, resettle_timer_updater, trainables, treshold_CIL, added_a_class, memory_buffer, chunk_number = train_epoch(model, task_embedding, trainables, optimizer, memory_buffer, storage_capacity,
                        loss_fun = loss_fun,
                        steps_per_epoch= steps_per_epoch,
                        X_train = X_train[i],
                        y_train = y_train[i],
                        X_test= X_test[i],
                        y_test = y_test[i],
                        train_accuracies = train_accuracies,
                        test_accuracies = test_accuracies,
                        class_incremental_case = class_incremental_case,
                        entropy_values = entropy_values,
                        use_unique_task_embedding = use_unique_task_embedding,
                        task_id = task_id,
                        final_soft_max_layer = final_soft_max_layer,
                        resettle_timer = resettle_timer,
                        resettle_timer_updater = resettle_timer_updater,
                        treshold_CIL = treshold_CIL,
                        amount_of_classes = amount_of_classes,
                        predictions = predictions,
                        training_while_testing = training_while_testing,
                        Average_GEM = Average_GEM,
                        margin = margin,
                        chunk_number =chunk_number)
        else:
            for j in range (max_attempts):
                final_accuracy, train_accuracies,test_accuracies, predictions, amount_of_classes, entropy_values, optimizer, resettle_timer_updater, trainables, treshold_CIL, added_a_class, memory_buffer, chunk_number = train_epoch(model, task_embedding, trainables, optimizer, memory_buffer, storage_capacity,
                    loss_fun = loss_fun,
                    steps_per_epoch= steps_per_epoch,
                    X_train = X_train[i],
                    y_train = y_train[i],
                    X_test= X_test[i],
                    y_test = y_test[i],
                    train_accuracies = train_accuracies,
                    test_accuracies = test_accuracies,
                    class_incremental_case = class_incremental_case,
                    entropy_values = entropy_values,
                    use_unique_task_embedding = use_unique_task_embedding,
                    task_id = task_id,
                    final_soft_max_layer = final_soft_max_layer,
                    resettle_timer = resettle_timer,
                    resettle_timer_updater = resettle_timer_updater,
                    treshold_CIL = treshold_CIL,
                    amount_of_classes = amount_of_classes,
                    predictions= predictions,
                    training_while_testing = training_while_testing,
                    Average_GEM =Average_GEM,
                    margin = margin,
                    chunk_number =chunk_number)
                if final_accuracy > validation_accuracy:
                    break
                elif j == (max_attempts-1):
                    print(f'Validation accuracy cannot be reached.')
                    break
                else:
                    #throw away results from the last run
                    train_accuracies = train_accuracies[:-len(X_train[i])]
                    test_accuracies = test_accuracies[:-len(X_test[i])]
                    print(f'Validation accuracy not reached. Repeating Task...')
    
    if class_incremental_case[0]:
        return train_accuracies, test_accuracies, predictions, amount_of_classes
    else:
        return train_accuracies, test_accuracies, predictions, None
    
def train_epoch(model,
                task_embedding,
                trainables, 
                optimizer,
                memory_buffer,
                storage_capacity,
                loss_fun = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
                steps_per_epoch = 5,
                X_train = np.random.rand(10, 128, 32, 1), 
                y_train = np.random.randint(0, 3, size=(10)),
                X_test=np.random.rand(5, 128, 32, 1),
                y_test = np.random.randint(0, 3, size=(5)),
                train_accuracies = [],
                test_accuracies = [],
                predictions = [],
                amount_of_classes = [],
                class_incremental_case = (False, None, None, False),
                entropy_values = [],
                use_unique_task_embedding = False,
                task_id = 0,
                final_soft_max_layer = False,
                resettle_timer = 0,
                resettle_timer_updater = 0,
                treshold_CIL = 0,
                training_while_testing = False,
                Average_GEM = False,
                margin = 0,
                chunk_number = (None, None)):

    Accu_train = tf.metrics.SparseCategoricalAccuracy()
    Loss_train = tf.metrics.SparseCategoricalCrossentropy()

    Accu_test = tf.metrics.SparseCategoricalAccuracy()
    Loss_test = tf.metrics.SparseCategoricalCrossentropy()

    train_data = list(zip(X_train, y_train))
    tbar = tqdm(islice(train_data, steps_per_epoch),
            total=steps_per_epoch,
            ascii=True)

    #assumption that every task has the same amount of images. This is only used for class incremental learning.
    if class_incremental_case[0]:
        images_processed_estimation = task_id* steps_per_epoch
    increase_classes_allowed = False #for the initial class incremental timer
    added_a_class = False

    predictions_training = []
    predictions_testing = []

    ''' MAIN TRAINING LOOP'''
    for epoch, (x, y) in enumerate(tbar):

        if class_incremental_case[0]:
            # Resettle time handling, if the resettle time has run out or no additional classes have been found yet
            if (resettle_timer_updater == 0) or (resettle_timer_updater == resettle_timer):
                resettle_timer_updater = resettle_timer
                increase_classes_allowed_resettle = True
            elif (resettle_timer_updater + 1) == resettle_timer:
                increase_classes_allowed_resettle = False
            else:
                increase_classes_allowed_resettle = False

            # Initial timer handling    
            images_processed_estimation = images_processed_estimation+1
            if ((class_incremental_case[2] -images_processed_estimation) < 0) and (increase_classes_allowed_resettle):
                increase_classes_allowed = True
            else:
                increase_classes_allowed = False

        outs, entropy_values, optimizer, increase_classes, trainables, memory_buffer, chunk_number = train_step(task_embedding, x.reshape(1, 128, 32, 1), y, model, 
                            loss_fun, trainables, optimizer, memory_buffer, storage_capacity,
                            class_incremental_case, use_unique_task_embedding, entropy_values, increase_classes = False,
                            increase_classes_allowed = increase_classes_allowed, final_soft_max_layer = final_soft_max_layer,
                            treshold_CIL = treshold_CIL, Average_GEM = Average_GEM, margin = margin, chunk_number = chunk_number)

        # Resettle time handling,
        if (class_incremental_case[0]) and (resettle_timer_updater > 0) and (increase_classes):
            resettle_timer_updater -= 1
        elif (class_incremental_case[0]) and (resettle_timer_updater != resettle_timer):
            resettle_timer_updater -= 1
        
        # Increase treshold for class incremental learning after finding one class
        if (class_incremental_case[0]) and increase_classes:
            treshold_CIL += class_incremental_case[4][1]
            added_a_class = True
            amount_of_classes.append((len(outs.numpy()), task_id+1, epoch))
            chunk_number = (True, -resettle_timer_updater)
            if class_incremental_case[4][1] != 0:
                print('\n the treshold for adding a new class just increased and is now {}'.format(round(treshold_CIL, 2)))
        elif (class_incremental_case[0]) and (not increase_classes):
            chunk_number = (chunk_number[0], chunk_number[1]+1)

        #update stats
        guessed_class = tf.argmax(outs).numpy()
        predictions_training.append(guessed_class)

        Accu_train.update_state(y, outs)
        Loss_train.update_state(y, outs)
        train_accuracies.append(Accu_train.result().numpy())

        if training_while_testing:
            if X_test[epoch] is not None:
                outs = test_step(model, task_embedding, x.reshape(1, 128, 32, 1), y_test[epoch])

                if class_incremental_case[0]: # we potentially miss a class
                    if int(y_test[epoch]) > (tf.size(outs).numpy() -1): # we definitely miss a class
                        #say that the probability assigned to this class was zero
                        zeros_tensor = tf.zeros(int(y_test[epoch]) - int(tf.size(outs).numpy() -1 ))
                        outs = tf.concat([outs, zeros_tensor], axis=0)

                #update stats
                guessed_class = tf.argmax(outs).numpy()
                Accu_test.update_state(y_test[epoch], outs)
                Loss_test.update_state(y_test[epoch], outs)
                test_accuracies.append(Accu_test.result().numpy())
                predictions_testing.append(guessed_class)
            else:
                test_accuracies.append(None)
    
    print(f'\n TRAIN: accuracy {Accu_train.result():6.3f}, loss {Loss_train.result():6.3f}')

    if training_while_testing:
        print(f' VALID: accuracy {Accu_test.result():6.3f}, loss {Loss_test.result():6.3f} \n')
        predictions.append((predictions_training, predictions_testing))
        return Accu_test.result(), train_accuracies, test_accuracies, predictions, amount_of_classes, entropy_values, optimizer, resettle_timer_updater, trainables, treshold_CIL, added_a_class, memory_buffer, chunk_number

    test_data = list(zip(X_test, y_test))
    for x, y in test_data:
        if x is None:
            test_accuracies.append(None)
            continue

        outs = test_step(model, task_embedding, x.reshape(1, 128, 32, 1), y_test[epoch])
        if class_incremental_case[0]:
            if int(y) > (tf.size(outs).numpy() -1):
                zeros_tensor = tf.zeros(int(y) - int(tf.size(outs).numpy() -1 ))
                outs = tf.concat([outs, zeros_tensor], axis=0)

        #update stats
        guessed_class = tf.argmax(outs).numpy()
        Accu_test.update_state(y, outs)
        Loss_test.update_state(y, outs)
        test_accuracies.append(Accu_test.result().numpy())
        predictions_testing.append(guessed_class)

    print(f' VALID: accuracy {Accu_test.result():6.3f}, loss {Loss_test.result():6.3f} \n')
    predictions.append((predictions_training, predictions_testing))
    return Accu_test.result(), train_accuracies, test_accuracies, predictions, amount_of_classes, entropy_values, optimizer, resettle_timer_updater, trainables, treshold_CIL, added_a_class, memory_buffer, chunk_number
    


if __name__ == "__main__":
    run()








